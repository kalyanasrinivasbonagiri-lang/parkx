"""Watch MongoDB changes and publish scoped WebSocket refresh events."""

import asyncio
import logging
from datetime import UTC, datetime
from threading import Event
from typing import Any

from bson import ObjectId
from pymongo.database import Database

from app.realtime.manager import ConnectionManager


logger = logging.getLogger(__name__)
WATCHED_COLLECTIONS = [
    "lots",
    "slots",
    "reservations",
    "bookings",
    "notifications",
]


def make_live_event(
    database: Database[dict[str, Any]], change: dict[str, Any]
) -> dict[str, str | None]:
    """Extract routing metadata without sending private document fields."""
    collection = change.get("ns", {}).get("coll")
    document = change.get("fullDocument") or {}
    document_id = change.get("documentKey", {}).get("_id") or document.get("_id")
    lot_id = document.get("lot_id")
    target_user_id = None

    if collection == "lots":
        lot_id = lot_id or document_id
    elif collection == "reservations" and lot_id is None:
        slot_id = document.get("slot_id")
        if slot_id is None and document_id is not None:
            reservation = database.reservations.find_one({"_id": document_id})
            slot_id = reservation.get("slot_id") if reservation else None
        if slot_id is not None:
            slot = database.slots.find_one({"_id": slot_id}, {"lot_id": 1})
            lot_id = slot.get("lot_id") if slot else None
    elif collection == "notifications":
        driver_id = document.get("driver_id")
        target_user_id = str(driver_id) if driver_id is not None else None

    return {
        "type": "data_changed",
        "collection": collection,
        "operation": change.get("operationType"),
        "lot_id": str(lot_id) if isinstance(lot_id, ObjectId) else lot_id,
        "document_id": str(document_id) if document_id is not None else None,
        "occurred_at": datetime.now(UTC).isoformat(),
        "target_user_id": target_user_id,
    }


def watch_change_stream(
    database: Database[dict[str, Any]],
    manager: ConnectionManager,
    event_loop: asyncio.AbstractEventLoop,
    stop_event: Event,
) -> None:
    """Consume change events in a worker thread and reconnect after failures."""
    pipeline = [{"$match": {"ns.coll": {"$in": WATCHED_COLLECTIONS}}}]
    resume_token: dict[str, Any] | None = None

    while not stop_event.is_set():
        try:
            # Atlas replica sets support change streams; updateLookup supplies the
            # changed document so events can be scoped to a lot or driver.
            watch_options: dict[str, Any] = {
                "full_document": "updateLookup",
                "max_await_time_ms": 1000,
            }
            if resume_token is not None:
                watch_options["resume_after"] = resume_token
            with database.watch(pipeline, **watch_options) as change_stream:
                logger.info("MongoDB change stream connected")
                while not stop_event.is_set():
                    change = change_stream.try_next()
                    if change_stream.resume_token is not None:
                        resume_token = change_stream.resume_token
                    if change is None:
                        continue
                    event = make_live_event(database, change)
                    future = asyncio.run_coroutine_threadsafe(
                        manager.broadcast(event), event_loop
                    )
                    try:
                        future.result(timeout=5)
                    except Exception:
                        logger.exception("Could not broadcast MongoDB change event")
        except Exception as error:
            if not stop_event.is_set():
                if getattr(error, "code", None) in {260, 286}:
                    # The server has discarded this token; start a fresh stream.
                    resume_token = None
                logger.exception("MongoDB change stream stopped; reconnecting")
                stop_event.wait(2)

