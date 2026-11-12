"""Atlas integration check: concurrent requests cannot hold one slot/hour twice."""

import json
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from bson import ObjectId

from app.auth import create_access_token, hash_password
from app.db import close_client, get_database


API_URL = "http://127.0.0.1:8000/api/bookings"


class BookingConcurrencyTest(unittest.TestCase):
    """Run two real HTTP requests against the configured local API and Atlas."""

    def test_only_one_request_holds_the_same_slot_and_hour(self) -> None:
        database = get_database()
        lot = database.lots.find_one()
        self.assertIsNotNone(lot, "Run the demo seed script before this integration check")

        suffix = uuid.uuid4().hex[:10].upper()
        driver_id = ObjectId()
        slot_id = ObjectId()
        email = f"booking-check-{suffix.lower()}@parkx.demo"
        driver = {
            "_id": driver_id,
            "name": "Concurrency Check",
            "email": email,
            "phone": "9000000000",
            "password_hash": hash_password("ConcurrencyCheck123!"),
            "vehicles": [f"TC{suffix}"],
            "role": "driver",
            "is_active": True,
        }
        slot = {
            "_id": slot_id,
            "lot_id": lot["_id"],
            "slot_number": f"T{suffix}",
            "slot_type": "car",
            "grid_position": {"row": 99, "column": 99, "floor": 0},
            "physical_state": "FREE",
        }
        database.users.insert_one(driver)
        try:
            database.slots.insert_one(slot)
            token = create_access_token(str(driver_id), "driver")
            now = datetime.now(UTC)
            next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
            request_body = json.dumps(
                {
                    "slot_id": str(slot_id),
                    "start_at": next_hour.isoformat(),
                    "hours": 1,
                    "vehicle_number": driver["vehicles"][0],
                }
            ).encode("utf-8")
            barrier = Barrier(2)

            def send_booking_request() -> int:
                barrier.wait()
                request = urllib.request.Request(
                    API_URL,
                    data=request_body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=15) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda _: send_booking_request(), range(2)))

            self.assertEqual(responses.count(201), 1, f"Expected one success, got {responses}")
            self.assertEqual(responses.count(409), 1, f"Expected one conflict, got {responses}")
        finally:
            database.bookings.delete_many({"driver_id": driver_id})
            database.reservations.delete_many({"slot_id": slot_id})
            database.slots.delete_one({"_id": slot_id})
            database.users.delete_one({"_id": driver_id})
            close_client()


if __name__ == "__main__":
    unittest.main()

