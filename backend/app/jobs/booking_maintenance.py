"""Periodic expiry, no-show, and in-app reminder jobs."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError


logger = logging.getLogger(__name__)
REMINDER_MINUTES = 15
MAINTENANCE_INTERVAL_SECONDS = 15
INDIA_TIME = timezone(timedelta(hours=5, minutes=30), name="IST")


def expire_payment_holds(
    database: Database[dict[str, Any]], now: datetime
) -> int:
    """Mark unpaid bookings expired and release their hourly reservations."""
    expired = list(
        database.bookings.find(
            {"status": "PENDING_PAYMENT", "hold_expires_at": {"$lte": now}},
            {"_id": 1},
        ).limit(100)
    )
    changed_count = 0
    for item in expired:
        with database.client.start_session() as session:
            with session.start_transaction():
                changed = database.bookings.update_one(
                    {
                        "_id": item["_id"],
                        "status": "PENDING_PAYMENT",
                        "hold_expires_at": {"$lte": now},
                    },
                    {"$set": {"status": "EXPIRED", "expired_at": now}},
                    session=session,
                )
                if changed.modified_count:
                    database.reservations.delete_many(
                        {"booking_id": item["_id"]}, session=session
                    )
                    changed_count += 1
    return changed_count


def expire_extension_holds(
    database: Database[dict[str, Any]], now: datetime
) -> int:
    """Expire unpaid extension holds and free only their added hour rows."""
    expired = list(
        database.extensions.find(
            {"status": "PENDING_PAYMENT", "hold_expires_at": {"$lte": now}},
            {"_id": 1},
        ).limit(100)
    )
    changed_count = 0
    for item in expired:
        with database.client.start_session() as session:
            with session.start_transaction():
                changed = database.extensions.update_one(
                    {
                        "_id": item["_id"],
                        "status": "PENDING_PAYMENT",
                        "hold_expires_at": {"$lte": now},
                    },
                    {"$set": {"status": "EXPIRED", "expired_at": now}},
                    session=session,
                )
                if changed.modified_count:
                    database.reservations.delete_many(
                        {"extension_id": item["_id"]}, session=session
                    )
                    changed_count += 1
    return changed_count


def mark_no_shows(database: Database[dict[str, Any]], now: datetime) -> int:
    """Mark confirmed bookings as no-show after the 15-minute arrival grace."""
    cutoff = now - timedelta(minutes=15)
    bookings = list(
        database.bookings.find(
            {"status": "CONFIRMED", "start_at": {"$lte": cutoff}},
            {"_id": 1},
        ).limit(100)
    )
    changed_count = 0
    for item in bookings:
        with database.client.start_session() as session:
            with session.start_transaction():
                changed = database.bookings.update_one(
                    {
                        "_id": item["_id"],
                        "status": "CONFIRMED",
                        "start_at": {"$lte": cutoff},
                    },
                    {"$set": {"status": "NO_SHOW", "no_show_at": now}},
                    session=session,
                )
                if changed.modified_count:
                    database.reservations.delete_many(
                        {"booking_id": item["_id"]}, session=session
                    )
                    changed_count += 1
    return changed_count


def queue_end_reminders(database: Database[dict[str, Any]], now: datetime) -> int:
    """Queue one in-app reminder for checked-in bookings ending within 15 minutes."""
    reminder_deadline = now + timedelta(minutes=REMINDER_MINUTES)
    due_bookings = database.bookings.find(
        {
            "status": "CHECKED_IN",
            "end_at": {"$gt": now, "$lte": reminder_deadline},
            "reminder_sent_at": {"$exists": False},
        }
    ).limit(100)

    created_count = 0
    for booking in due_bookings:
        reminder_time = (
            booking["end_at"].astimezone(INDIA_TIME).strftime("%I:%M %p").lstrip("0")
        )
        notification = {
            "booking_id": booking["_id"],
            "driver_id": booking["driver_id"],
            "kind": "BOOKING_ENDING_SOON",
            "message": f"Your parking booking ends at {reminder_time}. Please plan your exit.",
            "created_at": now,
        }
        try:
            with database.client.start_session() as session:
                with session.start_transaction():
                    updated = database.bookings.update_one(
                        {
                            "_id": booking["_id"],
                            "status": "CHECKED_IN",
                            "reminder_sent_at": {"$exists": False},
                        },
                        {"$set": {"reminder_sent_at": now}},
                        session=session,
                    )
                    if updated.modified_count:
                        database.notifications.insert_one(
                            notification, session=session
                        )
                        created_count += 1
        except DuplicateKeyError:
            # A second app worker may have queued the same reminder concurrently.
            continue
    return created_count


def run_maintenance_jobs(database: Database[dict[str, Any]]) -> dict[str, int]:
    """Run one pass of time-based booking maintenance."""
    now = datetime.now(UTC)
    return {
        "expired_bookings": expire_payment_holds(database, now),
        "expired_extensions": expire_extension_holds(database, now),
        "no_shows": mark_no_shows(database, now),
        "reminders": queue_end_reminders(database, now),
    }


async def maintenance_loop(database: Database[dict[str, Any]]) -> None:
    """Run periodic jobs without blocking FastAPI's event loop."""
    while True:
        try:
            results = await asyncio.to_thread(run_maintenance_jobs, database)
            if any(results.values()):
                logger.info("Booking maintenance updates: %s", results)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Booking maintenance job failed")
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)

