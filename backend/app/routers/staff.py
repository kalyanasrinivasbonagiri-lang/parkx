"""Employee routes for today's bookings and gate scans."""

import math
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any, Literal

import jwt
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, Field, model_validator
from pymongo import ReturnDocument
from pymongo.database import Database

from app.auth import get_current_user, require_roles
from app.config import get_settings
from app.db import get_database


router = APIRouter(
    prefix="/api/staff",
    tags=["staff"],
    dependencies=[Depends(require_roles("staff"))],
)

INDIA_TIME = timezone(timedelta(hours=5, minutes=30), name="IST")
ENTRY_GRACE_MINUTES = 15


class ScanRequest(BaseModel):
    """A QR scan or typed booking code and the intended gate action."""

    action: Literal["LOOKUP", "ENTRY", "EXIT"]
    qr_token: str | None = Field(default=None, min_length=1)
    booking_code: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def require_scan_value(self) -> "ScanRequest":
        """Require exactly one scan input, never both or neither."""
        if bool(self.qr_token) == bool(self.booking_code):
            raise ValueError("Provide either qr_token or booking_code")
        return self


def assigned_lot_id(employee: dict[str, Any]) -> ObjectId:
    """Return the employee's assigned lot id or deny access if it is missing."""
    lot_id = employee.get("assigned_lot_id")
    if not isinstance(lot_id, ObjectId):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This employee is not assigned to a lot",
        )
    return lot_id


def log_scan(
    database: Database[dict[str, Any]],
    employee: dict[str, Any],
    lot_id: ObjectId,
    action: str,
    result: str,
    booking_id: ObjectId | None = None,
    booking_code: str | None = None,
) -> None:
    """Record every valid or invalid employee scan for the audit trail."""
    database.scan_logs.insert_one(
        {
            "employee_id": employee["_id"],
            "lot_id": lot_id,
            "booking_id": booking_id,
            "booking_code": booking_code,
            "action": action,
            "result": result,
            "scanned_at": datetime.now(UTC),
        }
    )


def decode_scan_token(token: str) -> dict[str, Any]:
    """Verify QR signature while leaving time-window checks to booking rules."""
    settings = get_settings()
    return jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
        options={"verify_exp": False, "verify_nbf": False},
    )


def scan_result(
    database: Database[dict[str, Any]],
    employee: dict[str, Any],
    assigned_lot: ObjectId,
    request: ScanRequest,
    result: str,
    booking: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    scanned_booking_id: ObjectId | None = None,
) -> dict[str, Any]:
    """Write the audit row and format the result shown to the employee."""
    log_scan(
        database,
        employee,
        assigned_lot,
        request.action,
        result,
        booking["_id"] if booking else scanned_booking_id,
        (booking or {}).get("booking_code") or request.booking_code,
    )
    response: dict[str, Any] = {
        "valid": result in {"VALID", "ENTRY_CONFIRMED", "EXIT_COMPLETED", "OVERSTAY_DUE"},
        "result": result,
    }
    if details is not None:
        response["booking"] = details
    return response


def booking_details(
    database: Database[dict[str, Any]], booking: dict[str, Any]
) -> dict[str, Any]:
    """Return only the booking and driver details needed at the gate."""
    driver = database.users.find_one({"_id": booking["driver_id"]}) or {}
    slot = database.slots.find_one({"_id": booking["slot_id"]}) or {}
    lot = database.lots.find_one({"_id": booking["lot_id"]}) or {}
    details = {
        "booking_id": str(booking["_id"]),
        "booking_code": booking.get("booking_code"),
        "driver_name": driver.get("name", "Unknown driver"),
        "driver_phone": driver.get("phone"),
        "vehicle_number": booking.get("vehicle_number"),
        "lot_name": lot.get("name"),
        "slot_id": str(booking["slot_id"]),
        "slot_number": slot.get("slot_number"),
        "start_at": booking["start_at"].isoformat(),
        "end_at": booking["end_at"].isoformat(),
        "status": booking["status"],
    }
    if booking.get("status") == "CHECKED_IN" and booking.get("entry_at"):
        elapsed_seconds = max(
            0, int((datetime.now(UTC) - booking["entry_at"]).total_seconds())
        )
        details.update(
            {
                "entry_at": booking["entry_at"].isoformat(),
                "timer_running": True,
                "elapsed_seconds": elapsed_seconds,
            }
        )
    elif booking.get("status") in {"COMPLETED", "OVERSTAY_DUE"}:
        details["timer_running"] = False
        details["parking_minutes"] = booking.get("parking_minutes")
        details["overstay_amount"] = booking.get("overstay_amount", 0)
    return details


@router.get("/bookings/today")
def list_today_bookings(
    employee: dict[str, Any] = Depends(get_current_user),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """List bookings starting today at the employee's assigned lot."""
    lot_id = assigned_lot_id(employee)
    today = datetime.now(INDIA_TIME).date()
    day_start = datetime.combine(today, time.min, tzinfo=INDIA_TIME).astimezone(UTC)
    day_end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=INDIA_TIME).astimezone(UTC)
    bookings = list(
        database.bookings.find(
            {"lot_id": lot_id, "start_at": {"$gte": day_start, "$lt": day_end}}
        ).sort("start_at", 1)
    )
    return {
        "lot_id": str(lot_id),
        "date": today.isoformat(),
        "bookings": [booking_details(database, booking) for booking in bookings],
    }


@router.get("/slots")
def list_assigned_lot_slots(
    employee: dict[str, Any] = Depends(get_current_user),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """List physical slot states only for the employee's assigned lot."""
    lot_id = assigned_lot_id(employee)
    slots = list(
        database.slots.find({"lot_id": lot_id}).sort(
            [("grid_position.floor", 1), ("grid_position.row", 1), ("grid_position.column", 1)]
        )
    )
    now = datetime.now(UTC)
    current_hour = datetime.now(INDIA_TIME).replace(
        minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    slot_ids = [slot["_id"] for slot in slots]
    reservations = list(
        database.reservations.find(
            {
                "slot_id": {"$in": slot_ids},
                "hourStart": current_hour,
                "$or": [
                    {"status": {"$in": ["CONFIRMED", "CHECKED_IN"]}},
                    {"status": "PENDING_PAYMENT", "holdExpiresAt": {"$gt": now}},
                ],
            },
            {"slot_id": 1, "status": 1},
        )
    ) if slot_ids else []
    reservations_by_slot: dict[ObjectId, list[str]] = {}
    for reservation in reservations:
        reservations_by_slot.setdefault(reservation["slot_id"], []).append(
            reservation["status"]
        )

    def current_status(slot: dict[str, Any]) -> str:
        """Combine physical state and active reservations for the current hour."""
        physical_state = slot.get("physical_state", "FREE")
        active = reservations_by_slot.get(slot["_id"], [])
        if physical_state == "BLOCKED":
            return "BLOCKED"
        if physical_state == "OCCUPIED" or "CHECKED_IN" in active:
            return "OCCUPIED"
        if "PENDING_PAYMENT" in active:
            return "HELD"
        if "CONFIRMED" in active:
            return "BOOKED"
        return "FREE"

    return {
        "hour_start": current_hour.isoformat(),
        "slots": [
            {
                "id": str(slot["_id"]),
                "slot_number": slot["slot_number"],
                "slot_type": slot.get("slot_type", "car"),
                "physical_state": slot.get("physical_state", "FREE"),
                "status": current_status(slot),
                "grid_position": slot.get("grid_position", {}),
            }
            for slot in slots
        ]
    }


@router.post("/slots/{slot_id}/block")
def block_slot_for_maintenance(
    slot_id: str,
    employee: dict[str, Any] = Depends(get_current_user),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Block a free slot at the employee's assigned lot for maintenance."""
    lot_id = assigned_lot_id(employee)
    try:
        slot_object_id = ObjectId(slot_id)
    except InvalidId as error:
        raise HTTPException(status_code=404, detail="Slot not found") from error
    slot = database.slots.find_one({"_id": slot_object_id, "lot_id": lot_id})
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found at assigned lot")
    if slot.get("physical_state") != "FREE":
        raise HTTPException(status_code=409, detail="Only a free slot can be blocked")

    now = datetime.now(UTC)
    current_hour = datetime.now(INDIA_TIME).replace(
        minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    has_reservation = database.reservations.find_one(
        {
            "slot_id": slot_object_id,
            "hourStart": {"$gte": current_hour},
            "$or": [
                {"status": {"$in": ["CONFIRMED", "CHECKED_IN"]}},
                {"status": "PENDING_PAYMENT", "holdExpiresAt": {"$gt": now}},
            ],
        }
    )
    if has_reservation:
        raise HTTPException(
            status_code=409, detail="This slot has a current booking or payment hold"
        )

    updated = database.slots.find_one_and_update(
        {"_id": slot_object_id, "lot_id": lot_id, "physical_state": "FREE"},
        {"$set": {"physical_state": "BLOCKED"}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Slot state changed; refresh and retry")
    return {"slot_id": str(slot_object_id), "slot_number": slot["slot_number"], "physical_state": "BLOCKED"}


@router.post("/slots/{slot_id}/unblock")
def unblock_slot(
    slot_id: str,
    employee: dict[str, Any] = Depends(get_current_user),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Return a blocked slot at the employee's assigned lot to service."""
    lot_id = assigned_lot_id(employee)
    try:
        slot_object_id = ObjectId(slot_id)
    except InvalidId as error:
        raise HTTPException(status_code=404, detail="Slot not found") from error
    updated = database.slots.find_one_and_update(
        {"_id": slot_object_id, "lot_id": lot_id, "physical_state": "BLOCKED"},
        {"$set": {"physical_state": "FREE"}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Slot is not blocked")
    return {"slot_id": str(slot_object_id), "slot_number": updated["slot_number"], "physical_state": "FREE"}


@router.post("/scan")
def scan_booking(
    request: ScanRequest,
    employee: dict[str, Any] = Depends(get_current_user),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Look up a booking or confirm entry/exit, logging every scan attempt."""
    lot_id = assigned_lot_id(employee)
    claims: dict[str, Any] | None = None
    if request.qr_token:
        try:
            claims = decode_scan_token(request.qr_token)
        except InvalidTokenError:
            return scan_result(
                database, employee, lot_id, request, "INVALID_SIGNATURE"
            )
        if claims.get("purpose") != "parking_entry":
            return scan_result(database, employee, lot_id, request, "INVALID_TOKEN")
        try:
            booking_id = ObjectId(claims.get("booking_id", ""))
            token_lot_id = ObjectId(claims.get("lot_id", ""))
        except (InvalidId, TypeError):
            return scan_result(database, employee, lot_id, request, "INVALID_TOKEN")
        if token_lot_id != lot_id:
            return scan_result(
                database,
                employee,
                lot_id,
                request,
                "WRONG_LOT",
                scanned_booking_id=booking_id,
            )
        booking = database.bookings.find_one({"_id": booking_id, "lot_id": lot_id})
        if booking is None or booking.get("qr_token") != request.qr_token:
            return scan_result(
                database,
                employee,
                lot_id,
                request,
                "BOOKING_NOT_FOUND",
                scanned_booking_id=booking_id,
            )
    else:
        code = (request.booking_code or "").strip().upper()
        booking = database.bookings.find_one({"booking_code": code})
        if booking is None:
            return scan_result(
                database,
                employee,
                lot_id,
                request,
                "BOOKING_NOT_FOUND",
                booking_code=code,
            )
        booking_id = booking["_id"]
        if booking["lot_id"] != lot_id:
            return scan_result(
                database, employee, lot_id, request, "WRONG_LOT", booking
            )

    now = datetime.now(UTC)
    start_at = booking["start_at"]
    entry_opens_at = start_at - timedelta(minutes=ENTRY_GRACE_MINUTES)
    entry_closes_at = start_at + timedelta(minutes=ENTRY_GRACE_MINUTES)

    if request.action == "LOOKUP":
        if booking.get("status") == "CHECKED_IN":
            return scan_result(
                database,
                employee,
                lot_id,
                request,
                "VALID",
                booking,
                booking_details(database, booking),
            )
        if booking.get("status") != "CONFIRMED":
            result = "ALREADY_USED" if booking.get("status") in {"COMPLETED", "OVERSTAY_DUE", "NO_SHOW", "CANCELLED"} else "NOT_CONFIRMED"
            return scan_result(database, employee, lot_id, request, result, booking)
        qr_expires_at = booking.get("qr_expires_at")
        if request.qr_token and qr_expires_at is not None and now >= qr_expires_at:
            return scan_result(database, employee, lot_id, request, "QR_EXPIRED", booking)
        details = booking_details(database, booking)
        details.update(
            {
                "entry_allowed": entry_opens_at <= now < entry_closes_at,
                "entry_opens_at": entry_opens_at.isoformat(),
                "entry_closes_at": entry_closes_at.isoformat(),
            }
        )
        return scan_result(
            database,
            employee,
            lot_id,
            request,
            "VALID",
            booking,
            details,
        )

    if request.action == "ENTRY":
        if booking.get("status") != "CONFIRMED":
            result = "ALREADY_USED" if booking.get("status") != "PENDING_PAYMENT" else "NOT_CONFIRMED"
            return scan_result(database, employee, lot_id, request, result, booking)
        if now < entry_opens_at:
            return scan_result(database, employee, lot_id, request, "TOO_EARLY", booking)
        if now >= entry_closes_at:
            with database.client.start_session() as session:
                with session.start_transaction():
                    no_show_update = database.bookings.update_one(
                        {"_id": booking["_id"], "lot_id": lot_id, "status": "CONFIRMED"},
                        {"$set": {"status": "NO_SHOW", "no_show_at": now}},
                        session=session,
                    )
                    if no_show_update.modified_count:
                        database.reservations.delete_many(
                            {"booking_id": booking["_id"]}, session=session
                        )
            if no_show_update.modified_count:
                return scan_result(
                    database, employee, lot_id, request, "NO_SHOW", booking
                )
            latest = database.bookings.find_one({"_id": booking["_id"]}) or booking
            return scan_result(
                database, employee, lot_id, request, "ALREADY_USED", latest
            )

        entered_booking = None
        physical_slot_free = False
        with database.client.start_session() as session:
            with session.start_transaction():
                free_slot = database.slots.find_one_and_update(
                    {
                        "_id": booking["slot_id"],
                        "lot_id": lot_id,
                        "physical_state": "FREE",
                    },
                    {
                        "$set": {
                            "physical_state": "OCCUPIED",
                            "active_booking_id": booking["_id"],
                        }
                    },
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                physical_slot_free = free_slot is not None
                if physical_slot_free:
                    entered_booking = database.bookings.find_one_and_update(
                        {
                            "_id": booking["_id"],
                            "lot_id": lot_id,
                            "status": "CONFIRMED",
                            "start_at": {
                                "$gte": now - timedelta(minutes=ENTRY_GRACE_MINUTES),
                                "$lte": now + timedelta(minutes=ENTRY_GRACE_MINUTES),
                            },
                        },
                        {"$set": {"status": "CHECKED_IN", "entry_at": now}},
                        return_document=ReturnDocument.AFTER,
                        session=session,
                    )
                    if entered_booking is None:
                        database.slots.update_one(
                            {"_id": booking["slot_id"], "active_booking_id": booking["_id"]},
                            {
                                "$set": {"physical_state": "FREE"},
                                "$unset": {"active_booking_id": ""},
                            },
                            session=session,
                        )
                    else:
                        database.reservations.update_many(
                            {"booking_id": booking["_id"], "status": "CONFIRMED"},
                            {"$set": {"status": "CHECKED_IN"}},
                            session=session,
                        )
        if not physical_slot_free:
            return scan_result(database, employee, lot_id, request, "SLOT_NOT_FREE", booking)
        if entered_booking is None:
            latest = database.bookings.find_one({"_id": booking["_id"]}) or booking
            return scan_result(
                database, employee, lot_id, request, "ALREADY_USED", latest
            )
        details = booking_details(database, entered_booking)
        details["entry_at"] = now.isoformat()
        details["timer_started"] = True
        return scan_result(
            database,
            employee,
            lot_id,
            request,
            "ENTRY_CONFIRMED",
            entered_booking,
            details,
        )

    if booking.get("status") != "CHECKED_IN":
        result = "ALREADY_COMPLETED" if booking.get("status") in {"COMPLETED", "OVERSTAY_DUE"} else "NOT_CHECKED_IN"
        return scan_result(database, employee, lot_id, request, result, booking)

    exit_at = now
    booked_end = booking["end_at"]
    late_seconds = max(0.0, (exit_at - booked_end).total_seconds())
    extra_hours = math.ceil(late_seconds / 3600) if late_seconds else 0
    overstay_fee = round(
        extra_hours
        * float(booking.get("hourly_rate", 0))
        * float(booking.get("overstay_multiplier", 1.5)),
        2,
    )
    next_status = "OVERSTAY_DUE" if overstay_fee > 0 else "COMPLETED"
    exited_booking = None
    with database.client.start_session() as session:
        with session.start_transaction():
            exited_booking = database.bookings.find_one_and_update(
                {
                    "_id": booking["_id"],
                    "lot_id": lot_id,
                    "status": "CHECKED_IN",
                },
                {
                    "$set": {
                        "status": next_status,
                        "exit_at": exit_at,
                        "parking_minutes": max(
                            0,
                            math.ceil(
                                (exit_at - booking["entry_at"]).total_seconds() / 60
                            ),
                        ),
                        "overstay_hours": extra_hours,
                        "overstay_amount": overstay_fee,
                    }
                },
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if exited_booking is not None:
                database.slots.update_one(
                    {
                        "_id": booking["slot_id"],
                        "active_booking_id": booking["_id"],
                    },
                    {
                        "$set": {"physical_state": "FREE"},
                        "$unset": {"active_booking_id": ""},
                    },
                    session=session,
                )
                reservation_status = (
                    "COMPLETED" if exit_at >= booked_end else "CONFIRMED"
                )
                database.reservations.update_many(
                    {"booking_id": booking["_id"]},
                    {
                        "$set": {"status": reservation_status},
                        "$unset": {"holdExpiresAt": ""},
                    },
                    session=session,
                )
    if exited_booking is None:
        latest = database.bookings.find_one({"_id": booking["_id"]}) or booking
        return scan_result(
            database, employee, lot_id, request, "ALREADY_COMPLETED", latest
        )

    details = booking_details(database, exited_booking)
    details.update(
        {
            "entry_at": exited_booking["entry_at"].isoformat(),
            "exit_at": exit_at.isoformat(),
            "parking_minutes": exited_booking["parking_minutes"],
            "overstay_hours": extra_hours,
            "overstay_amount": overstay_fee,
        }
    )
    result = "OVERSTAY_DUE" if overstay_fee > 0 else "EXIT_COMPLETED"
    return scan_result(
        database, employee, lot_id, request, result, exited_booking, details
    )

