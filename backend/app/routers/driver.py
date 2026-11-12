"""Driver routes for browsing cities, lots, and slot availability."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.database import Database

from app.auth import require_roles
from app.db import get_database
from app.services.booking import hour_bucket_starts


router = APIRouter(
    prefix="/api/driver",
    tags=["driver browsing"],
    dependencies=[Depends(require_roles("driver"))],
)

ACTIVE_BOOKED_STATUSES = ["CONFIRMED"]
ACTIVE_OCCUPIED_STATUSES = ["CHECKED_IN", "OVERSTAY_DUE"]
ACTIVE_FIXED_STATUSES = ACTIVE_BOOKED_STATUSES + ACTIVE_OCCUPIED_STATUSES
SlotStatus = Literal["FREE", "HELD", "BOOKED", "OCCUPIED", "BLOCKED"]
INDIA_TIME = timezone(timedelta(hours=5, minutes=30), name="IST")
GRID_STATUSES: tuple[SlotStatus, ...] = (
    "FREE",
    "HELD",
    "BOOKED",
    "OCCUPIED",
    "BLOCKED",
)


def parse_lot_id(lot_id: str) -> ObjectId:
    """Parse a route lot id or report that the requested lot does not exist."""
    try:
        return ObjectId(lot_id)
    except InvalidId as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Lot not found"
        ) from error


def get_hour_window(
    start_at: datetime | None, hours: int
) -> tuple[datetime, list[datetime]]:
    """Build UTC hour keys touched by a minute-precise booking window."""
    if start_at is None:
        current = datetime.now(INDIA_TIME).replace(
            minute=0, second=0, microsecond=0
        ).astimezone(UTC)
    else:
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
        current = start_at.astimezone(UTC)

    return current, hour_bucket_starts(current, current + timedelta(hours=hours))


def find_active_reservations(
    database: Database[dict[str, Any]],
    slot_ids: list[ObjectId],
    hour_starts: list[datetime],
) -> list[dict[str, Any]]:
    """Load reservations that still hold or occupy a slot in the requested hours."""
    if not slot_ids or not hour_starts:
        return []

    now = datetime.now(UTC)
    # An unpaid hold can remain until the TTL monitor runs, so ignore it as soon
    # as its deadline passes even if MongoDB has not deleted it yet.
    active_filter = {
        "$or": [
            {"status": {"$in": ACTIVE_FIXED_STATUSES}},
            {
                "status": "PENDING_PAYMENT",
                "holdExpiresAt": {"$gt": now},
            },
        ]
    }
    return list(
        database.reservations.find(
            {
                "slot_id": {"$in": slot_ids},
                "hourStart": {"$in": hour_starts},
                **active_filter,
            },
            {"slot_id": 1, "hourStart": 1, "status": 1},
        )
    )


def summarize_slot(slot: dict[str, Any], reservations: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert the physical state and reservation records to a grid status."""
    physical_state = slot.get("physical_state", "FREE")
    matching = sorted(reservations, key=lambda item: item["hourStart"])
    if physical_state == "BLOCKED":
        display_status: SlotStatus = "BLOCKED"
    elif physical_state == "OCCUPIED":
        display_status = "OCCUPIED"
    elif any(item["status"] in ACTIVE_OCCUPIED_STATUSES for item in matching):
        display_status = "OCCUPIED"
    elif any(item["status"] == "PENDING_PAYMENT" for item in matching):
        display_status = "HELD"
    elif matching:
        display_status = "BOOKED"
    else:
        display_status = "FREE"

    position = slot.get("grid_position", {})
    return {
        "id": str(slot["_id"]),
        "slot_number": slot["slot_number"],
        "slot_type": slot.get("slot_type", "car"),
        "grid_position": {
            "row": position.get("row", 1),
            "column": position.get("column", 1),
            "floor": position.get("floor", 0),
        },
        "status": display_status,
        "available": display_status == "FREE",
        "conflict_hours": [item["hourStart"].isoformat() for item in matching],
    }


@router.get("/cities")
def list_cities(
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, list[str]]:
    """Return cities that currently have at least one parking lot."""
    cities = database.lots.distinct("city")
    return {"cities": sorted(city for city in cities if isinstance(city, str))}


@router.get("/cities/{city}/lots")
def list_city_lots(
    city: str,
    start_at: datetime | None = Query(
        default=None,
        description="Optional timezone-aware hourly start; defaults to the current hour.",
    ),
    hours: int = Query(default=1, ge=1, le=24),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """List city lots and free-slot counts for the selected hour window."""
    normalized_city = city.strip()
    if not normalized_city:
        raise HTTPException(status_code=422, detail="City cannot be empty")

    window_start, hour_starts = get_hour_window(start_at, hours)
    lots = list(
        database.lots.find({"city": normalized_city}).sort("name", 1)
    )
    lot_ids = [lot["_id"] for lot in lots]
    if not lots:
        return {"city": normalized_city, "start_at": window_start.isoformat(), "hours": hours, "lots": []}

    # Aggregate per-lot physical slot totals and free physical slots in MongoDB.
    slot_counts = {
        item["_id"]: item
        for item in database.slots.aggregate(
            [
                {"$match": {"lot_id": {"$in": lot_ids}}},
                {
                    "$group": {
                        "_id": "$lot_id",
                        "total_slots": {"$sum": 1},
                        "physical_free_slots": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$physical_state", "FREE"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                },
            ]
        )
    }

    slots = list(database.slots.find({"lot_id": {"$in": lot_ids}}))
    slot_by_id = {slot["_id"]: slot for slot in slots}
    reservations = find_active_reservations(
        database, list(slot_by_id), hour_starts
    )
    reserved_free_by_lot: dict[ObjectId, set[ObjectId]] = {}
    held_count_by_lot: dict[ObjectId, set[ObjectId]] = {}
    booked_count_by_lot: dict[ObjectId, set[ObjectId]] = {}
    for reservation in reservations:
        slot = slot_by_id.get(reservation["slot_id"])
        if slot is None:
            continue
        lot_id = slot["lot_id"]
        if slot.get("physical_state", "FREE") == "FREE":
            reserved_free_by_lot.setdefault(lot_id, set()).add(slot["_id"])
        if reservation["status"] == "PENDING_PAYMENT":
            held_count_by_lot.setdefault(lot_id, set()).add(slot["_id"])
        else:
            booked_count_by_lot.setdefault(lot_id, set()).add(slot["_id"])

    response_lots = []
    for lot in lots:
        lot_id = lot["_id"]
        counts = slot_counts.get(lot_id, {"total_slots": 0, "physical_free_slots": 0})
        total_slots = counts["total_slots"]
        free_slots = max(
            0,
            counts["physical_free_slots"]
            - len(reserved_free_by_lot.get(lot_id, set())),
        )
        location = lot.get("location", {})
        coordinates = location.get("coordinates", [None, None])
        response_lots.append(
            {
                "id": str(lot_id),
                "name": lot["name"],
                "city": lot["city"],
                "address": lot.get("address", ""),
                "location": {"longitude": coordinates[0], "latitude": coordinates[1]},
                "hourly_rate": lot.get("hourly_rate", 0),
                "total_slots": total_slots,
                "free_slots": free_slots,
                "held_slots": len(held_count_by_lot.get(lot_id, set())),
                "booked_slots": len(booked_count_by_lot.get(lot_id, set())),
            }
        )

    return {
        "city": normalized_city,
        "start_at": window_start.isoformat(),
        "hours": hours,
        "lots": response_lots,
    }


@router.get("/lots/{lot_id}/slots")
def get_slot_grid(
    lot_id: str,
    start_at: datetime = Query(
        description="Timezone-aware hourly start, for example 2026-11-12T18:00:00+05:30."
    ),
    hours: int = Query(default=1, ge=1, le=24),
    slot_type: Literal["car", "bike", "ev"] | None = Query(default=None),
    floor: int | None = Query(default=None, ge=0),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Return grid positions and availability for a lot and hourly window."""
    lot_object_id = parse_lot_id(lot_id)
    lot = database.lots.find_one({"_id": lot_object_id})
    if lot is None:
        raise HTTPException(status_code=404, detail="Lot not found")

    window_start, hour_starts = get_hour_window(start_at, hours)
    slot_filter: dict[str, Any] = {"lot_id": lot_object_id}
    if slot_type is not None:
        slot_filter["slot_type"] = slot_type
    if floor is not None:
        slot_filter["grid_position.floor"] = floor
    slots = list(
        database.slots.find(slot_filter).sort(
            [
                ("grid_position.floor", 1),
                ("grid_position.row", 1),
                ("grid_position.column", 1),
            ]
        )
    )
    reservations = find_active_reservations(
        database, [slot["_id"] for slot in slots], hour_starts
    )
    reservations_by_slot: dict[ObjectId, list[dict[str, Any]]] = {}
    for reservation in reservations:
        reservations_by_slot.setdefault(reservation["slot_id"], []).append(reservation)

    slot_results = [
        summarize_slot(slot, reservations_by_slot.get(slot["_id"], []))
        for slot in slots
    ]
    counts = {
        key: sum(slot["status"] == key for slot in slot_results)
        for key in GRID_STATUSES
    }
    return {
        "lot": {
            "id": str(lot["_id"]),
            "name": lot["name"],
            "city": lot["city"],
            "hourly_rate": lot.get("hourly_rate", 0),
        },
        "start_at": window_start.isoformat(),
        "hours": hours,
        "counts": counts,
        "slots": slot_results,
    }

