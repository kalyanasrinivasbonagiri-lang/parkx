"""Cancellation refunds and paid hourly booking extensions."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from bson import ObjectId
from fastapi import HTTPException, status
from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.config import get_settings
from app.services.booking import HOLD_MINUTES, hour_bucket_starts
from app.services.payment import QR_END_GRACE_MINUTES, create_qr_data_url


PaymentMethod = Literal["UPI", "CARD", "NET_BANKING"]
PaymentAction = Literal["PAY", "FAIL"]


def cancel_booking(
    database: Database[dict[str, Any]],
    booking_id: ObjectId,
    driver: dict[str, Any],
) -> dict[str, Any]:
    """Cancel a confirmed booking and record the policy refund in MongoDB."""
    booking_filter = {"_id": booking_id, "driver_id": driver["_id"]}
    booking = database.bookings.find_one(booking_filter)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.get("status") != "CONFIRMED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only confirmed bookings can be cancelled",
        )

    now = datetime.now(UTC)
    seconds_before_start = (booking["start_at"] - now).total_seconds()
    if seconds_before_start <= 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A booking cannot be cancelled after it starts",
        )

    # More than one hour receives 100%; exactly one hour falls into the 50% tier.
    refund_rate = 1.0 if seconds_before_start > 3600 else 0.5
    refund_amount = round(float(booking["amount"]) * refund_rate, 2)
    refund = {
        "_id": ObjectId(),
        "booking_id": booking_id,
        "driver_id": driver["_id"],
        "payment_type": "REFUND",
        "amount": refund_amount,
        "status": "REFUNDED",
        "reference": f"DEMO-REF-{uuid.uuid4().hex[:10].upper()}",
        "created_at": now,
    }

    with database.client.start_session() as session:
        with session.start_transaction():
            cancelled = database.bookings.find_one_and_update(
                {
                    **booking_filter,
                    "status": "CONFIRMED",
                    "start_at": {"$gt": now},
                },
                {
                    "$set": {
                        "status": "CANCELLED",
                        "cancelled_at": now,
                        "refund_amount": refund_amount,
                    }
                },
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if cancelled is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Booking changed and can no longer be cancelled",
                )
            database.reservations.delete_many(
                {"booking_id": booking_id}, session=session
            )
            database.payments.insert_one(refund, session=session)

    return {
        "booking_id": str(booking_id),
        "status": "CANCELLED",
        "refund_rate": refund_rate,
        "refund_amount": refund_amount,
        "refund_reference": refund["reference"],
    }


def create_booking_extension(
    database: Database[dict[str, Any]],
    booking_id: ObjectId,
    driver: dict[str, Any],
    extra_hours: int,
) -> dict[str, Any]:
    """Hold the next free hourly windows on the same slot for ten minutes."""
    booking = database.bookings.find_one(
        {"_id": booking_id, "driver_id": driver["_id"]}
    )
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.get("status") != "CHECKED_IN":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a checked-in booking can be extended",
        )

    now = datetime.now(UTC)
    if now >= booking["end_at"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Extension must be requested before the booked end time",
        )

    extension_id = ObjectId()
    starts_at = booking["end_at"]
    ends_at = starts_at + timedelta(hours=extra_hours)
    hourly_rate = float(booking["hourly_rate"])
    amount = round(hourly_rate * extra_hours, 2)
    hold_expires_at = now + timedelta(minutes=HOLD_MINUTES)
    extension = {
        "_id": extension_id,
        "booking_id": booking_id,
        "driver_id": driver["_id"],
        "lot_id": booking["lot_id"],
        "slot_id": booking["slot_id"],
        "start_at": starts_at,
        "end_at": ends_at,
        "extra_hours": extra_hours,
        "amount": amount,
        "status": "PENDING_PAYMENT",
        "created_at": now,
        "hold_expires_at": hold_expires_at,
    }
    hour_starts = hour_bucket_starts(starts_at, ends_at)
    reservations = [
        {
            "slot_id": booking["slot_id"],
            "lot_id": booking["lot_id"],
            "hourStart": hour_start,
            "booking_id": booking_id,
            "extension_id": extension_id,
            "driver_id": driver["_id"],
            "status": "PENDING_PAYMENT",
            "holdExpiresAt": hold_expires_at,
        }
        for hour_start in hour_starts
    ]

    try:
        with database.client.start_session() as session:
            with session.start_transaction():
                # Release a previous extension hold if its ten minutes already elapsed.
                expired_extensions = list(
                    database.extensions.find(
                        {
                            "booking_id": booking_id,
                            "status": "PENDING_PAYMENT",
                            "hold_expires_at": {"$lte": now},
                        },
                        {"_id": 1},
                        session=session,
                    )
                )
                expired_ids = [item["_id"] for item in expired_extensions]
                if expired_ids:
                    database.extensions.update_many(
                        {"_id": {"$in": expired_ids}},
                        {"$set": {"status": "EXPIRED", "expired_at": now}},
                        session=session,
                    )
                    database.reservations.delete_many(
                        {"extension_id": {"$in": expired_ids}}, session=session
                    )

                existing = database.extensions.find_one(
                    {"booking_id": booking_id, "status": "PENDING_PAYMENT"},
                    session=session,
                )
                if existing:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Finish or wait for the current extension hold first",
                    )

                database.extensions.insert_one(extension, session=session)
                # Reuse hour locks already held by this booking; unique locks protect new hours.
                existing_hour_starts = {
                    item["hourStart"]
                    for item in database.reservations.find(
                        {
                            "booking_id": booking_id,
                            "hourStart": {"$in": hour_starts},
                            "status": {"$in": ["CONFIRMED", "CHECKED_IN"]},
                        },
                        {"hourStart": 1},
                        session=session,
                    )
                }
                new_reservations = [
                    item for item in reservations
                    if item["hourStart"] not in existing_hour_starts
                ]
                # The unique slot/hour index rejects overlap with another booking.
                database.reservations.insert_many(new_reservations, session=session)
    except DuplicateKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="One or more following hours are already booked or held",
        ) from error
    except HTTPException:
        raise

    return {
        "extension_id": str(extension_id),
        "booking_id": str(booking_id),
        "start_at": starts_at.isoformat(),
        "end_at": ends_at.isoformat(),
        "extra_hours": extra_hours,
        "amount": amount,
        "status": "PENDING_PAYMENT",
        "hold_expires_at": hold_expires_at.isoformat(),
    }


def paid_extension_result(
    database: Database[dict[str, Any]],
    booking: dict[str, Any],
    extension_id: ObjectId,
) -> dict[str, Any] | None:
    """Return an already-paid extension result for safe repeated requests."""
    payment = database.payments.find_one(
        {
            "extension_id": extension_id,
            "payment_type": "EXTENSION",
            "status": "PAID",
        }
    )
    extension = database.extensions.find_one({"_id": extension_id})
    if payment is None or extension is None or extension.get("status") != "CONFIRMED":
        return None
    return {
        "booking_id": str(booking["_id"]),
        "extension_id": str(extension_id),
        "booking_status": booking["status"],
        "extension_status": extension["status"],
        "payment_status": payment["status"],
        "payment_method": payment["method"],
        "amount": payment["amount"],
        "reference": payment["reference"],
        "new_end_at": booking["end_at"].isoformat(),
        "qr_token": booking["qr_token"],
        "qr_code_data_url": booking["qr_code_data_url"],
        "qr_expires_at": booking["qr_expires_at"].isoformat(),
    }


def process_extension_payment(
    database: Database[dict[str, Any]],
    booking_id: ObjectId,
    extension_id: ObjectId,
    driver: dict[str, Any],
    method: PaymentMethod,
    action: PaymentAction,
) -> dict[str, Any]:
    """Pay for a held extension using its server-calculated amount."""
    booking_filter = {"_id": booking_id, "driver_id": driver["_id"]}
    extension_filter = {
        "_id": extension_id,
        "booking_id": booking_id,
        "driver_id": driver["_id"],
    }
    booking = database.bookings.find_one(booking_filter)
    extension = database.extensions.find_one(extension_filter)
    if booking is None or extension is None:
        raise HTTPException(status_code=404, detail="Booking extension not found")

    paid_result = paid_extension_result(database, booking, extension_id)
    if paid_result is not None:
        return paid_result
    if extension.get("status") != "PENDING_PAYMENT":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Extension cannot be paid while status is {extension.get('status')}",
        )

    now = datetime.now(UTC)
    if extension["hold_expires_at"] <= now:
        with database.client.start_session() as session:
            with session.start_transaction():
                database.extensions.update_one(
                    {"_id": extension_id, "status": "PENDING_PAYMENT"},
                    {"$set": {"status": "EXPIRED", "expired_at": now}},
                    session=session,
                )
                database.reservations.delete_many(
                    {"extension_id": extension_id}, session=session
                )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The extension hold expired; request the extension again",
        )

    payment_id = ObjectId()
    reference = f"DEMO-EXT-{uuid.uuid4().hex[:10].upper()}"
    payment = {
        "_id": payment_id,
        "booking_id": booking_id,
        "extension_id": extension_id,
        "driver_id": driver["_id"],
        "payment_type": "EXTENSION",
        "method": method,
        "amount": extension["amount"],
        "status": "FAILED" if action == "FAIL" else "PAID",
        "reference": reference,
        "created_at": now,
    }

    if action == "FAIL":
        with database.client.start_session() as session:
            with session.start_transaction():
                current = database.extensions.find_one_and_update(
                    {
                        **extension_filter,
                        "status": "PENDING_PAYMENT",
                        "hold_expires_at": {"$gt": datetime.now(UTC)},
                    },
                    {"$inc": {"payment_attempts": 1}},
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if current is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The extension hold expired or was already processed",
                    )
                database.payments.insert_one(payment, session=session)
        return {
            "booking_id": str(booking_id),
            "extension_id": str(extension_id),
            "extension_status": "PENDING_PAYMENT",
            "payment_id": str(payment_id),
            "payment_status": "FAILED",
            "amount": extension["amount"],
            "reference": reference,
            "hold_expires_at": extension["hold_expires_at"].isoformat(),
        }

    settings = get_settings()
    qr_expires_at = extension["end_at"] + timedelta(minutes=QR_END_GRACE_MINUTES)
    qr_token = jwt.encode(
        {
            "booking_id": str(booking_id),
            "lot_id": str(booking["lot_id"]),
            "booking_code": booking["booking_code"],
            "nbf": booking["start_at"] - timedelta(minutes=15),
            "exp": qr_expires_at,
            "iat": now,
            "purpose": "parking_entry",
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    qr_code_data_url = create_qr_data_url(qr_token)

    try:
        with database.client.start_session() as session:
            with session.start_transaction():
                transaction_now = datetime.now(UTC)
                confirmed_extension = database.extensions.find_one_and_update(
                    {
                        **extension_filter,
                        "status": "PENDING_PAYMENT",
                        "hold_expires_at": {"$gt": transaction_now},
                    },
                    {"$set": {"status": "CONFIRMED", "paid_at": transaction_now}},
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if confirmed_extension is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The extension hold expired or was already processed",
                    )
                updated_booking = database.bookings.find_one_and_update(
                    {
                        **booking_filter,
                        "status": "CHECKED_IN",
                        "end_at": extension["start_at"],
                    },
                    {
                        "$set": {
                            "end_at": extension["end_at"],
                            "qr_token": qr_token,
                            "qr_code_data_url": qr_code_data_url,
                            "qr_expires_at": qr_expires_at,
                        },
                        "$inc": {
                            "hours": extension["extra_hours"],
                            "amount": extension["amount"],
                        },
                        "$unset": {"reminder_sent_at": ""},
                    },
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if updated_booking is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The booking changed and this extension cannot be applied",
                    )
                database.reservations.update_many(
                    {"extension_id": extension_id, "status": "PENDING_PAYMENT"},
                    {
                        "$set": {"status": "CONFIRMED"},
                        "$unset": {"holdExpiresAt": ""},
                    },
                    session=session,
                )
                database.notifications.delete_many(
                    {"booking_id": booking_id, "kind": "BOOKING_ENDING_SOON"},
                    session=session,
                )
                database.payments.insert_one(payment, session=session)
    except DuplicateKeyError as error:
        latest_booking = database.bookings.find_one(booking_filter)
        if latest_booking is not None:
            paid_result = paid_extension_result(database, latest_booking, extension_id)
            if paid_result is not None:
                return paid_result
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This extension payment was already processed",
        ) from error
    except OperationFailure as error:
        if error.code == 112 or error.has_error_label("TransientTransactionError"):
            latest_booking = database.bookings.find_one(booking_filter)
            if latest_booking is not None:
                paid_result = paid_extension_result(database, latest_booking, extension_id)
                if paid_result is not None:
                    return paid_result
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The booking changed while extension payment was processing",
            ) from error
        raise

    return {
        "booking_id": str(booking_id),
        "extension_id": str(extension_id),
        "booking_status": updated_booking["status"],
        "extension_status": confirmed_extension["status"],
        "payment_id": str(payment_id),
        "payment_status": payment["status"],
        "payment_method": method,
        "amount": payment["amount"],
        "reference": reference,
        "new_end_at": updated_booking["end_at"].isoformat(),
        "qr_token": updated_booking["qr_token"],
        "qr_code_data_url": updated_booking["qr_code_data_url"],
        "qr_expires_at": updated_booking["qr_expires_at"].isoformat(),
    }


def process_overstay_payment(
    database: Database[dict[str, Any]],
    booking_id: ObjectId,
    driver: dict[str, Any],
    method: PaymentMethod,
    action: PaymentAction,
) -> dict[str, Any]:
    """Settle the server-calculated overstay fee and complete the booking."""
    booking_filter = {"_id": booking_id, "driver_id": driver["_id"]}
    booking = database.bookings.find_one(booking_filter)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    paid_payment = database.payments.find_one(
        {"booking_id": booking_id, "payment_type": "OVERSTAY", "status": "PAID"}
    )
    if booking.get("status") == "COMPLETED" and paid_payment is not None:
        return {
            "booking_id": str(booking_id),
            "booking_status": "COMPLETED",
            "payment_status": "PAID",
            "amount": paid_payment["amount"],
            "reference": paid_payment["reference"],
        }
    if booking.get("status") != "OVERSTAY_DUE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This booking has no outstanding overstay fee",
        )

    now = datetime.now(UTC)
    payment_id = ObjectId()
    reference = f"DEMO-OVR-{uuid.uuid4().hex[:10].upper()}"
    payment = {
        "_id": payment_id,
        "booking_id": booking_id,
        "driver_id": driver["_id"],
        "payment_type": "OVERSTAY",
        "method": method,
        "amount": booking["overstay_amount"],
        "status": "FAILED" if action == "FAIL" else "PAID",
        "reference": reference,
        "created_at": now,
    }

    try:
        with database.client.start_session() as session:
            with session.start_transaction():
                update: dict[str, Any]
                if action == "FAIL":
                    update = {"$inc": {"overstay_payment_attempts": 1}}
                else:
                    update = {
                        "$set": {
                            "status": "COMPLETED",
                            "overstay_paid_at": now,
                        }
                    }
                updated_booking = database.bookings.find_one_and_update(
                    {**booking_filter, "status": "OVERSTAY_DUE"},
                    update,
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if updated_booking is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The overstay fee was already processed",
                    )
                database.payments.insert_one(payment, session=session)
    except DuplicateKeyError as error:
        existing = database.payments.find_one(
            {"booking_id": booking_id, "payment_type": "OVERSTAY", "status": "PAID"}
        )
        latest_booking = database.bookings.find_one(booking_filter)
        if existing and latest_booking:
            return {
                "booking_id": str(booking_id),
                "booking_status": latest_booking["status"],
                "payment_status": existing["status"],
                "amount": existing["amount"],
                "reference": existing["reference"],
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The overstay payment was already processed",
        ) from error

    return {
        "booking_id": str(booking_id),
        "booking_status": updated_booking["status"],
        "payment_id": str(payment_id),
        "payment_status": payment["status"],
        "payment_method": method,
        "amount": payment["amount"],
        "reference": reference,
    }

