"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager
from threading import Event, Thread
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pymongo.database import Database

from app.db import close_client, get_database, initialize_database
from app.jobs.booking_maintenance import maintenance_loop
from app.realtime.change_stream import watch_change_stream
from app.realtime.manager import connection_manager
from app.realtime.routes import router as realtime_router
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.bookings import router as bookings_router
from app.routers.driver import router as driver_router
from app.routers.reports import router as reports_router
from app.routers.staff import router as staff_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Check MongoDB and prepare indexes and collections during startup."""
    database: Database[dict[str, Any]] = get_database()
    database.client.admin.command("ping")
    initialize_database(database)
    maintenance_task = asyncio.create_task(maintenance_loop(database))
    stop_change_stream = Event()
    change_stream_thread = Thread(
        target=watch_change_stream,
        args=(database, connection_manager, asyncio.get_running_loop(), stop_change_stream),
        daemon=True,
        name="mongo-change-stream",
    )
    change_stream_thread.start()
    try:
        yield
    finally:
        maintenance_task.cancel()
        stop_change_stream.set()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(change_stream_thread.join, 5)
        close_client()


app = FastAPI(title="Smart Parking Slot Booking System", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(driver_router)
app.include_router(bookings_router)
app.include_router(staff_router)
app.include_router(reports_router)
app.include_router(realtime_router)


@app.get("/health", tags=["health"])
def health_check() -> dict[str, str]:
    """Return a simple API liveness response."""
    return {"status": "ok"}

