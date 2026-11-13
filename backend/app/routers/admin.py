"""Admin-only endpoints for parking lots, slots, and employees."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from pymongo.database import Database
from pymongo.errors import BulkWriteError, DuplicateKeyError

from app.auth import hash_password, require_roles
from app.db import get_database


router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_roles("admin"))],
)

INDIA_TIME = timezone(timedelta(hours=5, minutes=30), name="IST")


class GeoPoint(BaseModel):
    """Map location using GeoJSON coordinate order: longitude, latitude."""

    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class LotCreate(BaseModel):
    """Fields required to create a parking lot."""

    name: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=80)
    address: str = Field(min_length=1, max_length=300)
    location: GeoPoint
    hourly_rate: float = Field(gt=0)
    overstay_multiplier: float = Field(default=1.5, gt=0)

    @field_validator("name", "city", "address")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        """Reject whitespace-only lot details and normalize saved text."""
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value


class SlotCreate(BaseModel):
    """One slot and its position in the visual grid."""

    slot_number: str = Field(min_length=1, max_length=20)
    slot_type: Literal["car", "bike", "ev"]
    row: int = Field(ge=1)
    column: int = Field(ge=1)
    floor: int = Field(default=0, ge=0)

    @field_validator("slot_number")
    @classmethod
    def slot_number_must_not_be_blank(cls, value: str) -> str:
        """Trim labels and reject blank slot numbers before bulk insert."""
        value = value.strip().upper()
        if not value:
            raise ValueError("Slot number cannot be blank")
        return value


class BulkSlotsCreate(BaseModel):
    """A list of slots to add to one lot in a single API call."""

    slots: list[SlotCreate] = Field(min_length=1, max_length=500)

    @field_validator("slots")
    @classmethod
    def slot_numbers_are_unique(cls, slots: list[SlotCreate]) -> list[SlotCreate]:
        """Reject duplicate slot labels before sending inserts to MongoDB."""
        numbers = [slot.slot_number.strip().upper() for slot in slots]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Slot numbers must be unique within a bulk request")
        return slots


class EmployeeCreate(BaseModel):
    """Fields an admin provides when creating a staff account."""

    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str = Field(min_length=7, max_length=20)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("name", "phone")
    @classmethod
    def employee_text_must_not_be_blank(cls, value: str) -> str:
        """Normalize employee text and reject whitespace-only values."""
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value


def object_id_or_404(value: str, resource_name: str) -> ObjectId:
    """Parse an id from a route and return 404 for invalid ids."""
    try:
        return ObjectId(value)
    except InvalidId as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_name} not found",
        ) from error


def serialize_lot(lot: dict[str, Any]) -> dict[str, Any]:
    """Return lot details with MongoDB ids converted to JSON strings."""
    return {
        "id": str(lot["_id"]),
        "name": lot["name"],
        "city": lot["city"],
        "address": lot["address"],
        "location": lot["location"],
        "hourly_rate": lot["hourly_rate"],
        "overstay_multiplier": lot["overstay_multiplier"],
    }


@router.post("/lots", status_code=status.HTTP_201_CREATED)
def create_lot(
    request: LotCreate,
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Create a lot with a GeoJSON point for map and nearby queries."""
    lot = {
        "name": request.name.strip(),
        "city": request.city.strip(),
        "address": request.address.strip(),
        "location": {
            "type": "Point",
            "coordinates": [request.location.longitude, request.location.latitude],
        },
        "hourly_rate": request.hourly_rate,
        "overstay_multiplier": request.overstay_multiplier,
    }
    result = database.lots.insert_one(lot)
    lot["_id"] = result.inserted_id
    return serialize_lot(lot)


@router.get("/lots")
def list_lots(
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """List lots for admin management and employee assignment forms."""
    lots = list(database.lots.find({}).sort([("city", 1), ("name", 1)]))
    return {"lots": [serialize_lot(lot) for lot in lots]}


@router.get("/lots/{lot_id}/details")
def get_lot_details(
    lot_id: str,
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Show the lot's current-hour counts and details for every parking slot."""
    lot_object_id = object_id_or_404(lot_id, "Lot")
    lot = database.lots.find_one({"_id": lot_object_id})
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")

    now = datetime.now(UTC)
    hour_start = datetime.now(INDIA_TIME).replace(
        minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    slots = list(database.slots.find({"lot_id": lot_object_id}))
    slots.sort(
        key=lambda item: (
            item.get("grid_position", {}).get("floor", 0),
            item.get("grid_position", {}).get("row", 0),
            item.get("grid_position", {}).get("column", 0),
            item.get("slot_number", ""),
        )
    )
    slot_ids = [slot["_id"] for slot in slots]

    # Read only active reservation rows for the current India-local hour bucket.
    reservations = list(database.reservations.find(
        {
            "slot_id": {"$in": slot_ids},
            "hourStart": hour_start,
            "$or": [
                {"status": {"$in": ["CONFIRMED", "CHECKED_IN"]}},
                {"status": "PENDING_PAYMENT", "holdExpiresAt": {"$gt": now}},
            ],
        },
        {"slot_id": 1, "booking_id": 1, "status": 1},
    )) if slot_ids else []

    reservations_by_slot: dict[ObjectId, list[dict[str, Any]]] = {}
    for reservation in reservations:
        reservations_by_slot.setdefault(reservation["slot_id"], []).append(reservation)
    booking_ids = list({item["booking_id"] for item in reservations if item.get("booking_id")})
    bookings = {
        booking["_id"]: booking
        for booking in database.bookings.find(
            {"_id": {"$in": booking_ids}},
            {"booking_code": 1, "vehicle_number": 1, "start_at": 1, "end_at": 1},
        )
    } if booking_ids else {}

    slot_details = []
    counts = {"FREE": 0, "HELD": 0, "BOOKED": 0, "OCCUPIED": 0, "BLOCKED": 0}
    for slot in slots:
        matching = reservations_by_slot.get(slot["_id"], [])
        physical_state = slot.get("physical_state", "FREE")
        if physical_state == "BLOCKED":
            current_status = "BLOCKED"
        elif physical_state == "OCCUPIED" or any(
            item.get("status") == "CHECKED_IN" for item in matching
        ):
            current_status = "OCCUPIED"
        elif any(item.get("status") == "PENDING_PAYMENT" for item in matching):
            current_status = "HELD"
        elif matching:
            current_status = "BOOKED"
        else:
            current_status = "FREE"
        counts[current_status] += 1

        reservation = matching[0] if matching else None
        booking = bookings.get(reservation.get("booking_id")) if reservation else None
        position = slot.get("grid_position", {})
        slot_details.append({
            "id": str(slot["_id"]),
            "slot_number": slot.get("slot_number", "—"),
            "slot_type": slot.get("slot_type", "car"),
            "physical_state": physical_state,
            "status": current_status,
            "floor": position.get("floor", 0),
            "row": position.get("row"),
            "column": position.get("column"),
            "booking_code": booking.get("booking_code") if booking else None,
            "vehicle_number": booking.get("vehicle_number") if booking else None,
            "start_at": booking.get("start_at").isoformat() if booking and booking.get("start_at") else None,
            "end_at": booking.get("end_at").isoformat() if booking and booking.get("end_at") else None,
        })

    return {
        "lot_id": str(lot_object_id),
        "name": lot["name"],
        "city": lot["city"],
        "address": lot["address"],
        "hourly_rate": lot["hourly_rate"],
        "overstay_multiplier": lot.get("overstay_multiplier", 1.5),
        "hour_start": hour_start.isoformat(),
        "total_slots": len(slots),
        "free_slots": counts["FREE"],
        "held_slots": counts["HELD"],
        "booked_slots": counts["BOOKED"],
        "occupied_slots": counts["OCCUPIED"],
        "blocked_slots": counts["BLOCKED"],
        "slots": slot_details,
    }


@router.post("/lots/{lot_id}/slots/bulk", status_code=status.HTTP_201_CREATED)
def create_slots(
    lot_id: str,
    request: BulkSlotsCreate,
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Create multiple positioned slots for an existing lot."""
    lot_object_id = object_id_or_404(lot_id, "Lot")
    if database.lots.find_one({"_id": lot_object_id}) is None:
        raise HTTPException(status_code=404, detail="Lot not found")

    slots = [
        {
            "lot_id": lot_object_id,
            "slot_number": slot.slot_number.strip().upper(),
            "slot_type": slot.slot_type,
            "grid_position": {
                "row": slot.row,
                "column": slot.column,
                "floor": slot.floor,
            },
            "physical_state": "FREE",
        }
        for slot in request.slots
    ]
    slot_numbers = [slot["slot_number"] for slot in slots]
    existing_count = database.slots.count_documents(
        {"lot_id": lot_object_id, "slot_number": {"$in": slot_numbers}}
    )
    if existing_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A slot number already exists for this lot",
        )
    try:
        result = database.slots.insert_many(slots, ordered=True)
    except (BulkWriteError, DuplicateKeyError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A slot number already exists for this lot",
        ) from error

    return {
        "created_count": len(result.inserted_ids),
        "slot_ids": [str(slot_id) for slot_id in result.inserted_ids],
    }


@router.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(
    request: EmployeeCreate,
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Create staff access without a permanent parking-lot assignment."""
    employee = {
        "name": request.name.strip(),
        "email": str(request.email).strip().lower(),
        "phone": request.phone.strip(),
        "password_hash": hash_password(request.password),
        "role": "staff",
        "is_active": True,
    }
    try:
        result = database.users.insert_one(employee)
    except DuplicateKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        ) from error

    return {
        "id": str(result.inserted_id),
        "name": employee["name"],
        "email": employee["email"],
        "role": employee["role"],
    }


@router.get("/employees")
def list_employees(
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """List staff accounts for admin management."""
    employees = list(database.users.find({"role": "staff"}).sort("name", 1))
    response = []
    for employee in employees:
        response.append(
            {
                "id": str(employee["_id"]),
                "name": employee["name"],
                "email": employee["email"],
                "phone": employee.get("phone"),
                "is_active": employee.get("is_active", True),
                "work_area": "Any lot",
            }
        )
    return {"employees": response}

