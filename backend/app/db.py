"""MongoDB client, collections, indexes, and time-series setup."""

from datetime import UTC, datetime
from typing import Any

from pymongo import ASCENDING, DESCENDING, GEOSPHERE, MongoClient
from pymongo.database import Database

from app.config import get_settings


_client: MongoClient[dict[str, Any]] | None = None


def get_client() -> MongoClient[dict[str, Any]]:
    """Return the shared MongoDB client, creating it on first use."""
    global _client
    if _client is None:
        settings = get_settings()
        # Read MongoDB dates as timezone-aware UTC values for business-rule checks.
        _client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=5000,
            tz_aware=True,
        )
    return _client


def get_database() -> Database[dict[str, Any]]:
    """Return the configured application database."""
    return get_client()[get_settings().mongodb_database]


def close_client() -> None:
    """Close the shared client during application shutdown."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def ensure_indexes(database: Database[dict[str, Any]]) -> None:
    """Create indexes used for uniqueness, lookup, expiry, and map queries."""
    # The unique slot/hour index is the database-level guard against double booking.
    database.reservations.create_index(
        [("slot_id", ASCENDING), ("hourStart", ASCENDING)],
        unique=True,
        name="unique_slot_hour",
    )

    # MongoDB TTL removes expired hold records after their holdExpiresAt timestamp.
    database.reservations.create_index(
        [("holdExpiresAt", ASCENDING)],
        expireAfterSeconds=0,
        name="reservation_hold_expiry_ttl",
        partialFilterExpression={"holdExpiresAt": {"$type": "date"}},
    )

    # City lookup and 2dsphere indexing support city browsing and nearby/map queries.
    database.lots.create_index([("city", ASCENDING)], name="lot_city")
    database.lots.create_index([("location", GEOSPHERE)], name="lot_location_2dsphere")

    database.slots.create_index(
        [("lot_id", ASCENDING), ("slot_number", ASCENDING)],
        unique=True,
        name="unique_lot_slot_number",
    )

    # A unique normalized email ensures one account per email address.
    database.users.create_index(
        [("email", ASCENDING)], unique=True, name="unique_user_email"
    )

    # Only one successful booking payment can exist for a booking.
    database.payments.create_index(
        [("booking_id", ASCENDING), ("payment_type", ASCENDING)],
        unique=True,
        partialFilterExpression={"payment_type": "BOOKING", "status": "PAID"},
        name="one_paid_booking_payment",
    )
    database.payments.create_index(
        [("extension_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"payment_type": "EXTENSION", "status": "PAID"},
        name="one_paid_extension_payment",
    )
    database.payments.create_index(
        [("booking_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"payment_type": "OVERSTAY", "status": "PAID"},
        name="one_paid_overstay_payment",
    )
    database.extensions.create_index(
        [("booking_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"status": "PENDING_PAYMENT"},
        name="one_pending_extension_per_booking",
    )
    database.notifications.create_index(
        [("booking_id", ASCENDING), ("kind", ASCENDING)],
        unique=True,
        name="one_reminder_per_booking_kind",
    )
    database.notifications.create_index(
        [("driver_id", ASCENDING), ("created_at", DESCENDING)],
        name="notifications_by_driver_and_time",
    )
    database.bookings.create_index(
        [("booking_code", ASCENDING)],
        unique=True,
        sparse=True,
        name="unique_booking_code",
    )
    database.bookings.create_index(
        [("lot_id", ASCENDING), ("start_at", ASCENDING)],
        name="bookings_by_lot_and_start",
    )
    # Driver history is sorted by creation time, so this index avoids a large sort.
    database.bookings.create_index(
        [("driver_id", ASCENDING), ("created_at", DESCENDING)],
        name="bookings_by_driver_and_created",
    )
    database.payments.create_index(
        [("booking_id", ASCENDING), ("created_at", ASCENDING)],
        name="payments_by_booking_and_created",
    )
    # Scan audit lookups are scoped by the employee's selected lot or by employee and time.
    database.scan_logs.create_index(
        [("lot_id", ASCENDING), ("scanned_at", DESCENDING)],
        name="scans_by_lot_and_time",
    )
    database.scan_logs.create_index(
        [("employee_id", ASCENDING), ("scanned_at", DESCENDING)],
        name="scans_by_employee_and_time",
    )


def ensure_occupancy_timeseries(database: Database[dict[str, Any]]) -> None:
    """Create occupancy history as a MongoDB time-series collection."""
    collection_name = "occupancy_history"
    if collection_name in database.list_collection_names():
        return

    # Time-series storage is suited to timestamped occupancy measurements per lot.
    database.create_collection(
        collection_name,
        timeseries={
            "timeField": "recorded_at",
            "metaField": "metadata",
            "granularity": "minutes",
        },
    )


def initialize_database(database: Database[dict[str, Any]]) -> None:
    """Set up collections and indexes before the API starts serving requests."""
    ensure_indexes(database)
    ensure_occupancy_timeseries(database)


def record_occupancy(
    database: Database[dict[str, Any]], lot_id: str, occupied_slots: int
) -> None:
    """Append one occupancy measurement for a lot."""
    database.occupancy_history.insert_one(
        {
            "recorded_at": datetime.now(UTC),
            "metadata": {"lot_id": lot_id},
            "occupied_slots": occupied_slots,
        }
    )

