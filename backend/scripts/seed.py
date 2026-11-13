"""Insert the repeatable demo data described in the Project Context PDF."""

from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.database import Database

from app.auth import hash_password
from app.db import close_client, get_database, initialize_database


DEMO_PASSWORD = "ParkxDemo123!"

LOT_DATA = [
    {
        "city": "Bengaluru",
        "name": "Majestic Parking",
        "address": "Kempegowda Bus Station, Bengaluru",
        "coordinates": [77.5713, 12.9767],
    },
    {
        "city": "Bengaluru",
        "name": "Forum Mall Parking",
        "address": "Forum Mall, Koramangala, Bengaluru",
        "coordinates": [77.6113, 12.9345],
    },
    {
        "city": "Mumbai",
        "name": "CST Parking",
        "address": "Chhatrapati Shivaji Maharaj Terminus, Mumbai",
        "coordinates": [72.8352, 18.9402],
    },
    {
        "city": "Mumbai",
        "name": "Phoenix Marketcity Parking",
        "address": "Phoenix Marketcity, Kurla, Mumbai",
        "coordinates": [72.8877, 19.0860],
    },
    {
        "city": "Hyderabad",
        "name": "Charminar Parking",
        "address": "Charminar, Hyderabad",
        "coordinates": [78.4747, 17.3616],
    },
    {
        "city": "Hyderabad",
        "name": "Inorbit Mall Parking",
        "address": "Inorbit Mall, Hitech City, Hyderabad",
        "coordinates": [78.3828, 17.4344],
    },
]

DRIVER_DATA = [
    {
        "name": "Rahul Sharma",
        "email": "rahul@parkx.demo",
        "phone": "9876543210",
        "vehicles": ["KA01AB1234"],
    },
    {
        "name": "Priya Rao",
        "email": "priya@parkx.demo",
        "phone": "9876543211",
        "vehicles": ["KA03CD5678"],
    },
    {
        "name": "Amit Patel",
        "email": "amit@parkx.demo",
        "phone": "9876543212",
        "vehicles": ["MH01EF9012"],
    },
]


def upsert_lot(database: Database[dict[str, Any]], data: dict[str, Any]) -> Any:
    """Find or create a lot by its demo city and name."""
    database.lots.update_one(
        {"city": data["city"], "name": data["name"]},
        {
            "$set": {
                "address": data["address"],
                "location": {"type": "Point", "coordinates": data["coordinates"]},
                "hourly_rate": 40.0,
                "overstay_multiplier": 1.5,
            }
        },
        upsert=True,
    )
    return database.lots.find_one({"city": data["city"], "name": data["name"]})["_id"]


def seed_slots(database: Database[dict[str, Any]], lot_id: Any) -> list[dict[str, Any]]:
    """Create 30 numbered slots with a mix of car, bike, and EV types."""
    slots: list[dict[str, Any]] = []
    for position in range(30):
        slot_type = "car" if position < 21 else "bike" if position < 27 else "ev"
        slot = {
            "lot_id": lot_id,
            "slot_number": f"A{position + 1}",
            "slot_type": slot_type,
            "grid_position": {
                "row": position // 6 + 1,
                "column": position % 6 + 1,
                "floor": 0,
            },
            "physical_state": "FREE",
        }
        database.slots.update_one(
            {"lot_id": lot_id, "slot_number": slot["slot_number"]},
            {"$setOnInsert": slot},
            upsert=True,
        )
        slots.append(database.slots.find_one({"lot_id": lot_id, "slot_number": slot["slot_number"]}))
    return slots


def upsert_user(
    database: Database[dict[str, Any]],
    user: dict[str, Any],
    role: str,
) -> Any:
    """Create a demo user once and retain any password changed after seeding."""
    user_doc: dict[str, Any] = {
        **user,
        "email": user["email"].lower(),
        "password_hash": hash_password(DEMO_PASSWORD),
        "role": role,
        "is_active": True,
    }
    if role == "driver":
        user_doc["vehicles"] = user.get("vehicles", [])
    database.users.update_one(
        {"email": user_doc["email"]},
        {"$setOnInsert": user_doc},
        upsert=True,
    )
    return database.users.find_one({"email": user_doc["email"]})["_id"]


def seed_bookings(
    database: Database[dict[str, Any]],
    lot_ids: list[Any],
    slots_by_lot: list[list[dict[str, Any]]],
    driver_ids: list[Any],
) -> None:
    """Create a few completed bookings and their hourly reservation rows."""
    now = datetime.now(UTC)
    samples = [
        (0, 0, 0, "KA01AB1234", 9, 11, 80.0),
        (0, 1, 1, "KA03CD5678", 12, 13, 40.0),
        (1, 2, 2, "MH01EF9012", 15, 17, 80.0),
    ]
    for number, (lot_index, slot_index, driver_index, vehicle, start_hour, end_hour, amount) in enumerate(samples, 1):
        booking_code = f"DEMO-PAST-{number}"
        booking_start = (now - timedelta(days=1)).replace(
            hour=start_hour, minute=0, second=0, microsecond=0
        )
        booking_end = booking_start + timedelta(hours=end_hour - start_hour)
        slot = slots_by_lot[lot_index][slot_index]
        booking_fields = {
            "booking_code": booking_code,
            "driver_id": driver_ids[driver_index],
            "lot_id": lot_ids[lot_index],
            "slot_id": slot["_id"],
            "start_at": booking_start,
            "end_at": booking_end,
            "amount": amount,
            "status": "COMPLETED",
            "vehicle_number": vehicle,
            "entry_at": booking_start,
            "exit_at": booking_end,
        }
        database.bookings.update_one(
            {"booking_code": booking_code}, {"$setOnInsert": booking_fields}, upsert=True
        )
        booking_id = database.bookings.find_one({"booking_code": booking_code})["_id"]
        database.payments.update_one(
            {
                "booking_id": booking_id,
                "payment_type": "BOOKING",
                "status": "PAID",
            },
            {
                "$setOnInsert": {
                    "booking_id": booking_id,
                    "driver_id": driver_ids[driver_index],
                    "payment_type": "BOOKING",
                    "method": "UPI",
                    "amount": amount,
                    "status": "PAID",
                    "reference": f"DEMO-PAST-PAY-{number}",
                    "created_at": booking_start - timedelta(minutes=5),
                }
            },
            upsert=True,
        )

        # Reservation rows demonstrate the same one-document-per-slot-per-hour design.
        hour = booking_start
        while hour < booking_end:
            database.reservations.update_one(
                {"slot_id": slot["_id"], "hourStart": hour},
                {
                    "$setOnInsert": {
                        "slot_id": slot["_id"],
                        "lot_id": lot_ids[lot_index],
                        "hourStart": hour,
                        "booking_id": booking_id,
                        "status": "COMPLETED",
                    }
                },
                upsert=True,
            )
            hour += timedelta(hours=1)


def seed_occupancy(database: Database[dict[str, Any]], lot_ids: list[Any]) -> None:
    """Add a few historical time-series readings for each lot once."""
    now = datetime.now(UTC)
    for lot_index, lot_id in enumerate(lot_ids):
        if database.occupancy_history.find_one(
            {"metadata.seed_key": f"demo-lot-{lot_id}"}
        ):
            continue
        for reading_index, hours_ago in enumerate((48, 24, 12, 4, 1)):
            database.occupancy_history.insert_one(
                {
                    "recorded_at": now - timedelta(hours=hours_ago),
                    "metadata": {
                        "lot_id": str(lot_id),
                        "seed_key": f"demo-lot-{lot_id}",
                    },
                    "occupied_slots": 6 + (lot_index * 3 + reading_index * 4) % 22,
                }
            )


def seed_demo_data(database: Database[dict[str, Any]]) -> None:
    """Insert or update all repeatable sample data for a project demo."""
    lot_ids = [upsert_lot(database, lot) for lot in LOT_DATA]
    slots_by_lot = [seed_slots(database, lot_id) for lot_id in lot_ids]

    upsert_user(
        database,
        {
            "name": "ParkX Admin",
            "email": "admin@parkx.demo",
            "phone": "9876500000",
        },
        "admin",
    )

    # Staff accounts can select their current work area after signing in.
    upsert_user(
        database,
        {"name": "Meena Staff", "email": "meena@parkx.demo", "phone": "9876500001"},
        "staff",
    )
    upsert_user(
        database,
        {"name": "Arjun Staff", "email": "arjun@parkx.demo", "phone": "9876500002"},
        "staff",
    )

    driver_ids = [upsert_user(database, driver, "driver") for driver in DRIVER_DATA]
    seed_bookings(database, lot_ids, slots_by_lot, driver_ids)
    seed_occupancy(database, lot_ids)


def main() -> None:
    """Connect to Atlas, initialize MongoDB structures, and seed demo content."""
    database = get_database()
    try:
        database.client.admin.command("ping")
        initialize_database(database)
        seed_demo_data(database)
        print("Demo data is ready. Demo password for all seeded accounts:", DEMO_PASSWORD)
        print("Admin: admin@parkx.demo")
        print("Staff: meena@parkx.demo, arjun@parkx.demo")
        print("Drivers: rahul@parkx.demo, priya@parkx.demo, amit@parkx.demo")
    finally:
        close_client()


if __name__ == "__main__":
    main()

