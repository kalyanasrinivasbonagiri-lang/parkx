"""Admin dashboard counts, business reports, and scan audit summaries."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from app.auth import require_roles
from app.db import get_database


router = APIRouter(
    prefix="/api/admin",
    tags=["admin reports"],
    dependencies=[Depends(require_roles("admin"))],
)

INDIA_TIME = timezone(timedelta(hours=5, minutes=30), name="IST")
ACTIVE_BOOKING_STATUSES = ["CONFIRMED", "CHECKED_IN"]
REPORT_BOOKING_STATUSES = [
    "CONFIRMED",
    "CHECKED_IN",
    "COMPLETED",
    "OVERSTAY_DUE",
    "NO_SHOW",
]
VALID_SCAN_RESULTS = ["VALID", "ENTRY_CONFIRMED", "EXIT_COMPLETED", "OVERSTAY_DUE"]


def parse_lot_filter(lot_id: str | None) -> ObjectId | None:
    """Convert an optional lot filter into a MongoDB id."""
    if lot_id is None:
        return None
    try:
        return ObjectId(lot_id)
    except InvalidId as error:
        raise HTTPException(status_code=404, detail="Lot not found") from error


def report_date_range(
    date_from: date | None, date_to: date | None
) -> tuple[datetime, datetime]:
    """Convert inclusive local calendar dates to a UTC half-open interval."""
    today = datetime.now(INDIA_TIME).date()
    first_day = date_from or (today - timedelta(days=29))
    last_day = date_to or today
    if first_day > last_day:
        raise HTTPException(status_code=422, detail="date_from must not be after date_to")

    start = datetime.combine(first_day, time.min, tzinfo=INDIA_TIME).astimezone(UTC)
    end = datetime.combine(
        last_day + timedelta(days=1), time.min, tzinfo=INDIA_TIME
    ).astimezone(UTC)
    return start, end


def slot_status(
    slot: dict[str, Any], reservations: list[dict[str, Any]]
) -> str:
    """Return one live status from a slot's physical and reservation states."""
    physical_state = slot.get("physical_state", "FREE")
    if physical_state == "BLOCKED":
        return "BLOCKED"
    if physical_state == "OCCUPIED":
        return "OCCUPIED"
    if any(item.get("status") == "CHECKED_IN" for item in reservations):
        return "OCCUPIED"
    if any(item.get("status") == "PENDING_PAYMENT" for item in reservations):
        return "HELD"
    if reservations:
        return "BOOKED"
    return "FREE"


@router.get("/dashboard/occupancy")
def get_lot_occupancy(
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Return current booked, free, held, occupied, and blocked counts per lot."""
    now = datetime.now(UTC)
    current_hour = datetime.now(INDIA_TIME).replace(
        minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    lots = list(database.lots.find({}).sort([("city", ASCENDING), ("name", ASCENDING)]))
    lot_ids = [lot["_id"] for lot in lots]

    # MongoDB groups physical slot counts per lot before reservation overlays.
    physical_counts = {
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

    all_slots = list(database.slots.find({"lot_id": {"$in": lot_ids}})) if lot_ids else []
    slot_by_id = {slot["_id"]: slot for slot in all_slots}
    if slot_by_id:
        reservations = list(
            database.reservations.find(
                {
                    "slot_id": {"$in": list(slot_by_id)},
                    "hourStart": current_hour,
                    "$or": [
                        {"status": {"$in": ACTIVE_BOOKING_STATUSES}},
                        {
                            "status": "PENDING_PAYMENT",
                            "holdExpiresAt": {"$gt": now},
                        },
                    ],
                },
                {"slot_id": 1, "status": 1},
            )
        )
    else:
        reservations = []

    reservations_by_slot: dict[ObjectId, list[dict[str, Any]]] = {}
    for reservation in reservations:
        reservations_by_slot.setdefault(reservation["slot_id"], []).append(reservation)

    counts_by_lot: dict[ObjectId, dict[str, int]] = {}
    for slot in all_slots:
        counts = counts_by_lot.setdefault(
            slot["lot_id"],
            {"FREE": 0, "HELD": 0, "BOOKED": 0, "OCCUPIED": 0, "BLOCKED": 0},
        )
        counts[slot_status(slot, reservations_by_slot.get(slot["_id"], []))] += 1

    response_lots = []
    for lot in lots:
        lot_id = lot["_id"]
        counts = counts_by_lot.get(
            lot_id, {"FREE": 0, "HELD": 0, "BOOKED": 0, "OCCUPIED": 0, "BLOCKED": 0}
        )
        physical = physical_counts.get(lot_id, {"total_slots": 0})
        response_lots.append(
            {
                "lot_id": str(lot_id),
                "name": lot["name"],
                "city": lot["city"],
                "total_slots": physical["total_slots"],
                "free_slots": counts["FREE"],
                "held_slots": counts["HELD"],
                "booked_slots": counts["BOOKED"],
                "occupied_slots": counts["OCCUPIED"],
                "blocked_slots": counts["BLOCKED"],
            }
        )

    return {"as_of": now.isoformat(), "lots": response_lots}


@router.get("/reports/summary")
def get_report_summary(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    lot_id: str | None = Query(default=None),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Aggregate booking totals, no-show rate, and average parking duration."""
    start, end = report_date_range(date_from, date_to)
    lot_object_id = parse_lot_filter(lot_id)
    match: dict[str, Any] = {
        "start_at": {"$gte": start, "$lt": end},
    }
    if lot_object_id is not None:
        match["lot_id"] = lot_object_id

    # Aggregation calculates settled durations and no-show rates in MongoDB.
    results = list(
        database.bookings.aggregate(
            [
                {"$match": match},
                {
                    "$group": {
                        "_id": None,
                        "total_bookings": {"$sum": 1},
                        "completed": {
                            "$sum": {"$cond": [{"$eq": ["$status", "COMPLETED"]}, 1, 0]}
                        },
                        "no_shows": {
                            "$sum": {"$cond": [{"$eq": ["$status", "NO_SHOW"]}, 1, 0]}
                        },
                        "eligible_arrivals": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$status", REPORT_BOOKING_STATUSES]},
                                            {"$lte": ["$start_at", datetime.now(UTC)]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                        "average_duration_minutes": {
                            "$avg": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$ne": [{"$ifNull": ["$entry_at", None]}, None]},
                                            {"$ne": [{"$ifNull": ["$exit_at", None]}, None]},
                                        ]
                                    },
                                    {
                                        "$divide": [
                                            {"$subtract": ["$exit_at", "$entry_at"]},
                                            60000,
                                        ]
                                    },
                                    None,
                                ]
                            }
                        },
                    }
                },
            ]
        )
    )
    summary = results[0] if results else {}
    total_due = summary.get("eligible_arrivals", 0)
    no_shows = summary.get("no_shows", 0)
    average_minutes = summary.get("average_duration_minutes")
    return {
        "date_from": date_from.isoformat() if date_from else start.astimezone(INDIA_TIME).date().isoformat(),
        "date_to": date_to.isoformat() if date_to else (end - timedelta(microseconds=1)).astimezone(INDIA_TIME).date().isoformat(),
        "total_bookings": summary.get("total_bookings", 0),
        "completed": summary.get("completed", 0),
        "no_shows": no_shows,
        "no_show_rate_percent": round((no_shows / total_due) * 100, 2) if total_due else 0,
        "average_duration_minutes": round(average_minutes, 2) if average_minutes is not None else 0,
    }


@router.get("/reports/revenue-by-day")
def get_revenue_by_day(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    lot_id: str | None = Query(default=None),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Report paid revenue and simulated refunds by lot and local calendar day."""
    start, end = report_date_range(date_from, date_to)
    lot_object_id = parse_lot_filter(lot_id)
    # Join payment rows to bookings to group revenue under the booking's lot.
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "created_at": {"$gte": start, "$lt": end},
                "status": {"$in": ["PAID", "REFUNDED"]},
            }
        },
        {
            "$lookup": {
                "from": "bookings",
                "localField": "booking_id",
                "foreignField": "_id",
                "as": "booking",
            }
        },
        {"$unwind": "$booking"},
    ]
    if lot_object_id is not None:
        pipeline.append({"$match": {"booking.lot_id": lot_object_id}})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": {
                        "lot_id": "$booking.lot_id",
                        "day": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$created_at",
                                "timezone": "Asia/Kolkata",
                            }
                        },
                    },
                    "paid_amount": {
                        "$sum": {"$cond": [{"$eq": ["$status", "PAID"]}, "$amount", 0]}
                    },
                    "refund_amount": {
                        "$sum": {"$cond": [{"$eq": ["$status", "REFUNDED"]}, "$amount", 0]}
                    },
                    "net_revenue": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "REFUNDED"]},
                                {"$multiply": ["$amount", -1]},
                                "$amount",
                            ]
                        }
                    },
                }
            },
            {"$lookup": {"from": "lots", "localField": "_id.lot_id", "foreignField": "_id", "as": "lot"}},
            {"$unwind": "$lot"},
            {"$sort": {"_id.day": 1, "lot.city": 1, "lot.name": 1}},
        ]
    )
    rows = [
        {
            "lot_id": str(row["_id"]["lot_id"]),
            "lot_name": row["lot"]["name"],
            "city": row["lot"]["city"],
            "day": row["_id"]["day"],
            "paid_amount": round(row["paid_amount"], 2),
            "refund_amount": round(row["refund_amount"], 2),
            "net_revenue": round(row["net_revenue"], 2),
        }
        for row in database.payments.aggregate(pipeline)
    ]
    return {"rows": rows}


@router.get("/reports/peak-hours")
def get_peak_hours(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    lot_id: str | None = Query(default=None),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Count valid booked windows by hour of the day in India time."""
    start, end = report_date_range(date_from, date_to)
    match: dict[str, Any] = {
        "start_at": {"$gte": start, "$lt": end},
        "status": {"$in": REPORT_BOOKING_STATUSES},
    }
    lot_object_id = parse_lot_filter(lot_id)
    if lot_object_id is not None:
        match["lot_id"] = lot_object_id

    # $hour with a timezone groups UTC datetimes into local India clock hours.
    rows = list(
        database.bookings.aggregate(
            [
                {"$match": match},
                {
                    "$group": {
                        "_id": {"$hour": {"date": "$start_at", "timezone": "Asia/Kolkata"}},
                        "bookings": {"$sum": 1},
                    }
                },
                {"$sort": {"bookings": -1, "_id": 1}},
            ]
        )
    )
    return {"hours": [{"hour": row["_id"], "bookings": row["bookings"]} for row in rows]}


@router.get("/reports/most-used-slots")
def get_most_used_slots(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    lot_id: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Rank slots by paid or active booking count for the selected period."""
    start, end = report_date_range(date_from, date_to)
    match: dict[str, Any] = {
        "start_at": {"$gte": start, "$lt": end},
        "status": {"$in": REPORT_BOOKING_STATUSES},
    }
    lot_object_id = parse_lot_filter(lot_id)
    if lot_object_id is not None:
        match["lot_id"] = lot_object_id
    rows = list(
        database.bookings.aggregate(
            [
                {"$match": match},
                {"$group": {"_id": "$slot_id", "booking_count": {"$sum": 1}}},
                {"$lookup": {"from": "slots", "localField": "_id", "foreignField": "_id", "as": "slot"}},
                {"$unwind": "$slot"},
                {"$lookup": {"from": "lots", "localField": "slot.lot_id", "foreignField": "_id", "as": "lot"}},
                {"$unwind": "$lot"},
                {"$sort": {"booking_count": -1, "slot.slot_number": 1}},
                {"$limit": limit},
            ]
        )
    )
    return {
        "slots": [
            {
                "slot_id": str(row["_id"]),
                "slot_number": row["slot"]["slot_number"],
                "slot_type": row["slot"].get("slot_type"),
                "lot_id": str(row["lot"]["_id"]),
                "lot_name": row["lot"]["name"],
                "city": row["lot"]["city"],
                "booking_count": row["booking_count"],
            }
            for row in rows
        ]
    }


@router.get("/reports/scans")
def get_scan_audit(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    lot_id: str | None = Query(default=None),
    employee_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    database: Database[dict[str, Any]] = Depends(get_database),
) -> dict[str, Any]:
    """Return recent scan audit rows and counts by employee and result."""
    start, end = report_date_range(date_from, date_to)
    match: dict[str, Any] = {"scanned_at": {"$gte": start, "$lt": end}}
    lot_object_id = parse_lot_filter(lot_id)
    if lot_object_id is not None:
        match["lot_id"] = lot_object_id
    if employee_id is not None:
        try:
            match["employee_id"] = ObjectId(employee_id)
        except InvalidId as error:
            raise HTTPException(status_code=404, detail="Employee not found") from error

    logs = list(
        database.scan_logs.find(match)
        .sort("scanned_at", DESCENDING)
        .limit(limit)
    )
    totals = list(
        database.scan_logs.aggregate(
            [
                {"$match": match},
                {
                    "$group": {
                        "_id": {"employee_id": "$employee_id", "result": "$result"},
                        "count": {"$sum": 1},
                    }
                },
                {"$sort": {"_id.employee_id": 1, "count": -1}},
            ]
        )
    )
    summary: dict[str, dict[str, Any]] = {}
    total_scans = 0
    invalid_scans = 0
    for row in totals:
        employee_key = str(row["_id"]["employee_id"])
        count = row["count"]
        total_scans += count
        if row["_id"]["result"] not in VALID_SCAN_RESULTS:
            invalid_scans += count
        employee_summary = summary.setdefault(
            employee_key, {"employee_id": employee_key, "total_scans": 0, "invalid_scans": 0}
        )
        employee_summary["total_scans"] += count
        if row["_id"]["result"] not in VALID_SCAN_RESULTS:
            employee_summary["invalid_scans"] += count

    employee_ids = [ObjectId(key) for key in summary]
    employees = {
        str(user["_id"]): user.get("name", "Unknown employee")
        for user in database.users.find({"_id": {"$in": employee_ids}}, {"name": 1})
    }
    for key, item in summary.items():
        item["employee_name"] = employees.get(key, "Unknown employee")

    return {
        "total_scans": total_scans,
        "invalid_scans": invalid_scans,
        "invalid_scan_rate_percent": round(invalid_scans / total_scans * 100, 2) if total_scans else 0,
        "by_employee": list(summary.values()),
        "logs": [
            {
                "id": str(log["_id"]),
                "employee_id": str(log["employee_id"]),
                "lot_id": str(log["lot_id"]),
                "booking_id": str(log["booking_id"]) if log.get("booking_id") else None,
                "booking_code": log.get("booking_code"),
                "action": log["action"],
                "result": log["result"],
                "scanned_at": log["scanned_at"].isoformat(),
            }
            for log in logs
        ],
    }

