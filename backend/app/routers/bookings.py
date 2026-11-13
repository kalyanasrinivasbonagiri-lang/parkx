"""Driver endpoint for reserving a slot during simulated payment."""

from datetime import datetime
from typing import Any, Literal

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from pymongo.database import Database

from app.auth import require_roles
from app.db import get_database
from app.services.booking import create_booking_hold
from app.services.booking_lifecycle import (
    cancel_booking,
    create_booking_extension,
    process_extension_payment,
    process_overstay_payment,
)
from app.services.payment import process_booking_payment


router = APIRouter(
    prefix="/api/bookings",
    tags=["bookings"],
    dependencies=[Depends(require_roles("driver"))],
)


class BookingCreate(BaseModel):
    """Driver-selected slot, minute-precise start, duration, and saved vehicle."""

    slot_id: str
    start_at: datetime
    hours: int = Field(ge=1, le=24)
    vehicle_number: str = Field(min_length=1, max_length=20)

    @field_validator("vehicle_number")
    @classmethod
    def normalize_vehicle(cls, value: str) -> str:
        """Trim and normalize a license plate for comparison and storage."""
        return value.strip().upper()


class SimulatedPaymentRequest(BaseModel):
    """Payment method and demo outcome; the client cannot set the amount."""

    method: Literal["UPI", "CARD", "NET_BANKING"]
    action: Literal["PAY", "FAIL"]


class BookingExtensionRequest(BaseModel):
    """Number of following hourly windows requested by a checked-in driver."""

    hours: int = Field(ge=1, le=24)


@router.post("", status_code=status.HTTP_201_CREATED)
def book_slot(
    request: BookingCreate,
    driver: dict[str, Any] = Depends(require_roles("driver")),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Create a ten-minute hold and hourly reservation rows for a driver."""
    try:
        slot_id = ObjectId(request.slot_id)
    except InvalidId as error:
        raise HTTPException(status_code=404, detail="Slot not found") from error

    return create_booking_hold(
        database=database,
        driver=driver,
        slot_id=slot_id,
        start_at=request.start_at,
        hours=request.hours,
        vehicle_number=request.vehicle_number,
    )


@router.get("/mine")
def list_my_bookings(
    driver: dict[str, Any] = Depends(require_roles("driver")),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Return the signed-in driver's booking history and payment receipts."""
    bookings = list(
        database.bookings.find({"driver_id": driver["_id"]})
        .sort("created_at", -1)
        .limit(100)
    )
    booking_ids = [booking["_id"] for booking in bookings]
    lot_ids = list({booking["lot_id"] for booking in bookings})
    slot_ids = list({booking["slot_id"] for booking in bookings})
    # Fetch related records in batches to avoid three Atlas round trips per booking.
    lots_by_id = {
        lot["_id"]: lot for lot in database.lots.find({"_id": {"$in": lot_ids}})
    } if lot_ids else {}
    slots_by_id = {
        slot["_id"]: slot for slot in database.slots.find({"_id": {"$in": slot_ids}})
    } if slot_ids else {}
    payments_by_booking: dict[ObjectId, list[dict[str, Any]]] = {}
    for payment in database.payments.find(
        {"booking_id": {"$in": booking_ids}}
    ).sort("created_at", 1) if booking_ids else []:
        payments_by_booking.setdefault(payment["booking_id"], []).append(payment)

    response = []
    for booking in bookings:
        lot = lots_by_id.get(booking["lot_id"], {})
        slot = slots_by_id.get(booking["slot_id"], {})
        payments = payments_by_booking.get(booking["_id"], [])
        response.append(
            {
                "booking_id": str(booking["_id"]),
                "booking_code": booking.get("booking_code"),
                "lot_id": str(booking["lot_id"]),
                "lot_name": lot.get("name"),
                "city": lot.get("city"),
                "address": lot.get("address"),
                "location": lot.get("location"),
                "slot_id": str(booking["slot_id"]),
                "slot_number": slot.get("slot_number"),
                "vehicle_number": booking.get("vehicle_number"),
                "start_at": booking["start_at"].isoformat(),
                "end_at": booking["end_at"].isoformat(),
                "hours": booking.get("hours", 1),
                "amount": booking.get("amount", 0),
                "status": booking["status"],
                "hold_expires_at": booking.get("hold_expires_at").isoformat()
                if booking.get("hold_expires_at")
                else None,
                "qr_token": booking.get("qr_token"),
                "qr_code_data_url": booking.get("qr_code_data_url"),
                "qr_expires_at": booking.get("qr_expires_at").isoformat()
                if booking.get("qr_expires_at")
                else None,
                "entry_at": booking.get("entry_at").isoformat()
                if booking.get("entry_at")
                else None,
                "exit_at": booking.get("exit_at").isoformat()
                if booking.get("exit_at")
                else None,
                "parking_minutes": booking.get("parking_minutes"),
                "overstay_hours": booking.get("overstay_hours", 0),
                "overstay_amount": booking.get("overstay_amount", 0),
                "refund_amount": booking.get("refund_amount", 0),
                "payments": [
                    {
                        "payment_type": payment["payment_type"],
                        "method": payment.get("method"),
                        "amount": payment["amount"],
                        "status": payment["status"],
                        "reference": payment.get("reference"),
                        "created_at": payment["created_at"].isoformat(),
                    }
                    for payment in payments
                ],
            }
        )
    return {"bookings": response}


@router.post("/{booking_id}/payment")
def pay_for_booking(
    booking_id: str,
    request: SimulatedPaymentRequest,
    driver: dict[str, Any] = Depends(require_roles("driver")),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Simulate booking payment and return the signed QR after success."""
    try:
        booking_object_id = ObjectId(booking_id)
    except InvalidId as error:
        raise HTTPException(status_code=404, detail="Booking not found") from error
    return process_booking_payment(
        database,
        booking_object_id,
        driver,
        request.method,
        request.action,
    )


@router.post("/{booking_id}/cancel")
def cancel_confirmed_booking(
    booking_id: str,
    driver: dict[str, Any] = Depends(require_roles("driver")),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Cancel a confirmed upcoming booking and record its policy refund."""
    try:
        booking_object_id = ObjectId(booking_id)
    except InvalidId as error:
        raise HTTPException(status_code=404, detail="Booking not found") from error
    return cancel_booking(database, booking_object_id, driver)


@router.post("/{booking_id}/extensions")
def extend_booking(
    booking_id: str,
    request: BookingExtensionRequest,
    driver: dict[str, Any] = Depends(require_roles("driver")),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Hold following free hours on the same slot for simulated payment."""
    try:
        booking_object_id = ObjectId(booking_id)
    except InvalidId as error:
        raise HTTPException(status_code=404, detail="Booking not found") from error
    return create_booking_extension(
        database, booking_object_id, driver, request.hours
    )


@router.post("/{booking_id}/extensions/{extension_id}/payment")
def pay_for_extension(
    booking_id: str,
    extension_id: str,
    request: SimulatedPaymentRequest,
    driver: dict[str, Any] = Depends(require_roles("driver")),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Pay for a held extension using its server-calculated amount."""
    try:
        booking_object_id = ObjectId(booking_id)
        extension_object_id = ObjectId(extension_id)
    except InvalidId as error:
        raise HTTPException(
            status_code=404, detail="Booking extension not found"
        ) from error
    return process_extension_payment(
        database,
        booking_object_id,
        extension_object_id,
        driver,
        request.method,
        request.action,
    )


@router.post("/{booking_id}/overstay-payment")
def pay_overstay_fee(
    booking_id: str,
    request: SimulatedPaymentRequest,
    driver: dict[str, Any] = Depends(require_roles("driver")),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Pay the overstay amount calculated and stored by the server at exit."""
    try:
        booking_object_id = ObjectId(booking_id)
    except InvalidId as error:
        raise HTTPException(status_code=404, detail="Booking not found") from error
    return process_overstay_payment(
        database,
        booking_object_id,
        driver,
        request.method,
        request.action,
    )


@router.get("/reminders")
def get_driver_reminders(
    driver: dict[str, Any] = Depends(require_roles("driver")),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Return in-app booking-end reminders queued for the signed-in driver."""
    reminders = list(
        database.notifications.find({"driver_id": driver["_id"]})
        .sort("created_at", -1)
        .limit(50)
    )
    return {
        "reminders": [
            {
                "id": str(reminder["_id"]),
                "booking_id": str(reminder["booking_id"]),
                "kind": reminder["kind"],
                "message": reminder["message"],
                "created_at": reminder["created_at"].isoformat(),
            }
            for reminder in reminders
        ]
    }

