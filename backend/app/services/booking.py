"""Booking hold creation using MongoDB transactions and unique hour rows."""

from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from fastapi import HTTPException, status
from pymongo.database import Database
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure


HOLD_MINUTES = 10
OVERSTAY_MULTIPLIER = 1.5
RELEASED_STATUSES = ["EXPIRED", "CANCELLED", "NO_SHOW", "COMPLETED"]


def hour_bucket_starts(start_at: datetime, end_at: datetime) -> list[datetime]:
    """Return every UTC clock-hour key touched by a booking interval."""
    hour_start = start_at.replace(minute=0, second=0, microsecond=0)
    hour_starts = []
    while hour_start < end_at:
        hour_starts.append(hour_start)
        hour_start += timedelta(hours=1)
    return hour_starts


def make_hour_starts(start_at: datetime, hours: int) -> tuple[datetime, list[datetime]]:
    """Validate minute-precise start time and return its covered UTC hour keys."""
    if start_at.tzinfo is None or start_at.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_at must include a timezone offset, such as +05:30",
        )
    if start_at.second != 0 or start_at.microsecond != 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_at must be precise to the minute",
        )

    start_utc = start_at.astimezone(UTC)
    if start_utc <= datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Booking must start in the future",
        )
    return start_utc, hour_bucket_starts(start_utc, start_utc + timedelta(hours=hours))


def create_booking_hold(
    database: Database[dict[str, Any]],
    driver: dict[str, Any],
    slot_id: ObjectId,
    start_at: datetime,
    hours: int,
    vehicle_number: str,
) -> dict[str, Any]:
    """Atomically create a booking and locks for each covered clock hour."""
    start_utc, hour_starts = make_hour_starts(start_at, hours)
    slot = database.slots.find_one({"_id": slot_id})
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found")
    if slot.get("physical_state", "FREE") != "FREE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This slot is not available",
        )

    vehicle = vehicle_number.strip().upper()
    saved_vehicles = [item.upper() for item in driver.get("vehicles", [])]
    if vehicle not in saved_vehicles:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Choose a vehicle saved in your driver profile",
        )

    lot = database.lots.find_one({"_id": slot["lot_id"]})
    if lot is None:
        raise HTTPException(status_code=404, detail="Parking lot not found")

    hourly_rate = float(lot["hourly_rate"])
    amount = round(hourly_rate * hours, 2)
    now = datetime.now(UTC)
    hold_expires_at = now + timedelta(minutes=HOLD_MINUTES)
    booking_id = ObjectId()
    booking = {
        "_id": booking_id,
        "driver_id": driver["_id"],
        "lot_id": lot["_id"],
        "slot_id": slot["_id"],
        "vehicle_number": vehicle,
        "start_at": start_utc,
        "end_at": start_utc + timedelta(hours=hours),
        "hours": hours,
        "hourly_rate": hourly_rate,
        # Section 7 fixes the demo overstay calculation at rate multiplied by 1.5.
        "overstay_multiplier": OVERSTAY_MULTIPLIER,
        "amount": amount,
        "status": "PENDING_PAYMENT",
        "created_at": now,
        "hold_expires_at": hold_expires_at,
    }
    reservations = [
        {
            "slot_id": slot["_id"],
            "lot_id": lot["_id"],
            "hourStart": hour_start,
            "booking_id": booking_id,
            "driver_id": driver["_id"],
            "status": "PENDING_PAYMENT",
            "holdExpiresAt": hold_expires_at,
        }
        for hour_start in hour_starts
    ]

    try:
        # Booking and hour rows commit together, so no partial hold can be left behind.
        with database.client.start_session() as session:
            with session.start_transaction():
                # TTL cleanup is asynchronous; clear expired and terminal rows now so
                # a stale reservation cannot block a new booking after its release.
                database.reservations.delete_many(
                    {
                        "slot_id": slot["_id"],
                        "hourStart": {"$in": hour_starts},
                        "$or": [
                            {
                                "status": "PENDING_PAYMENT",
                                "holdExpiresAt": {"$lte": now},
                            },
                            {"status": {"$in": RELEASED_STATUSES}},
                        ],
                    },
                    session=session,
                )
                database.bookings.insert_one(booking, session=session)
                # The unique (slot_id, hourStart) index is the final concurrency guard.
                database.reservations.insert_many(reservations, session=session)
    except DuplicateKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This slot was just taken for one or more selected hours",
        ) from error
    except BulkWriteError as error:
        # insert_many reports unique-index races as bulk write errors, not duplicate-key errors.
        write_errors = (error.details or {}).get("writeErrors", [])
        if any(item.get("code") == 11000 for item in write_errors):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This slot was just taken for one or more selected hours",
            ) from error
        raise
    except OperationFailure as error:
        if error.code == 112 or error.has_error_label("TransientTransactionError"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This slot was just taken for one or more selected hours",
            ) from error
        raise

    return {
        "booking_id": str(booking_id),
        "lot_id": str(lot["_id"]),
        "slot_id": str(slot["_id"]),
        "slot_number": slot["slot_number"],
        "start_at": booking["start_at"].isoformat(),
        "end_at": booking["end_at"].isoformat(),
        "hours": hours,
        "hourly_rate": hourly_rate,
        "amount": amount,
        "status": booking["status"],
        "hold_expires_at": hold_expires_at.isoformat(),
    }

