"""Server-controlled simulated booking payment and signed QR generation."""

import base64
import io
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
import qrcode
from bson import ObjectId
from fastapi import HTTPException, status
from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.config import get_settings


QR_END_GRACE_MINUTES = 15
PaymentMethod = Literal["UPI", "CARD", "NET_BANKING"]
PaymentAction = Literal["PAY", "FAIL"]


def create_qr_data_url(token: str) -> str:
    """Render a signed token into a PNG data URL for the confirmation screen."""
    image = qrcode.make(token)
    image_bytes = io.BytesIO()
    image.save(image_bytes, format="PNG")
    encoded_image = base64.b64encode(image_bytes.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded_image}"


def build_paid_response(
    booking: dict[str, Any], payment: dict[str, Any]
) -> dict[str, Any]:
    """Return the original paid result when a successful request is repeated."""
    return {
        "booking_id": str(booking["_id"]),
        "booking_code": booking["booking_code"],
        "booking_status": booking["status"],
        "payment_id": str(payment["_id"]),
        "payment_status": payment["status"],
        "payment_method": payment["method"],
        "amount": payment["amount"],
        "reference": payment["reference"],
        "qr_token": booking["qr_token"],
        "qr_code_data_url": booking["qr_code_data_url"],
        "qr_expires_at": booking["qr_expires_at"].isoformat(),
    }


def expire_booking_and_release(
    database: Database[dict[str, Any]], booking: dict[str, Any], now: datetime
) -> None:
    """Mark an unpaid booking expired and release its hour rows transactionally."""
    with database.client.start_session() as session:
        with session.start_transaction():
            database.bookings.update_one(
                {
                    "_id": booking["_id"],
                    "driver_id": booking["driver_id"],
                    "status": "PENDING_PAYMENT",
                    "hold_expires_at": {"$lte": now},
                },
                {"$set": {"status": "EXPIRED", "expired_at": now}},
                session=session,
            )
            database.reservations.delete_many(
                {"booking_id": booking["_id"]}, session=session
            )


def find_paid_result(
    database: Database[dict[str, Any]],
    booking_filter: dict[str, Any],
) -> dict[str, Any] | None:
    """Look up a committed payment after a concurrent request changes the booking."""
    payment = database.payments.find_one(
        {"booking_id": booking_filter["_id"], "payment_type": "BOOKING", "status": "PAID"}
    )
    booking = database.bookings.find_one(booking_filter)
    if payment is not None and booking is not None:
        return build_paid_response(booking, payment)
    return None


def process_booking_payment(
    database: Database[dict[str, Any]],
    booking_id: ObjectId,
    driver: dict[str, Any],
    method: PaymentMethod,
    action: PaymentAction,
) -> dict[str, Any]:
    """Process a demo payment using only the amount stored on the booking."""
    booking_filter = {"_id": booking_id, "driver_id": driver["_id"]}
    booking = database.bookings.find_one(booking_filter)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    paid_result = find_paid_result(database, booking_filter)
    if paid_result is not None:
        return paid_result
    if booking.get("status") != "PENDING_PAYMENT":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Booking cannot be paid while status is {booking.get('status')}",
        )

    now = datetime.now(UTC)
    if booking["hold_expires_at"] <= now:
        expire_booking_and_release(database, booking, now)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The payment hold expired; this booking is no longer payable",
        )

    payment_id = ObjectId()
    reference = f"DEMO-{uuid.uuid4().hex[:10].upper()}"
    payment_status = "FAILED" if action == "FAIL" else "PAID"
    payment = {
        "_id": payment_id,
        "booking_id": booking_id,
        "driver_id": driver["_id"],
        "payment_type": "BOOKING",
        "method": method,
        "amount": booking["amount"],
        "status": payment_status,
        "reference": reference,
        "created_at": now,
    }

    settings = get_settings()
    booking_code = f"PX-{uuid.uuid4().hex[:8].upper()}"
    qr_expires_at = booking["end_at"] + timedelta(minutes=QR_END_GRACE_MINUTES)
    qr_claims = {
        "booking_id": str(booking_id),
        "lot_id": str(booking["lot_id"]),
        "booking_code": booking_code,
        # Entry opens 15 minutes early and QR validity ends 15 minutes after booking.
        "nbf": booking["start_at"] - timedelta(minutes=15),
        "exp": qr_expires_at,
        "iat": now,
        "purpose": "parking_entry",
    }
    qr_token = jwt.encode(
        qr_claims, settings.jwt_secret_key, algorithm=settings.jwt_algorithm
    )
    qr_code_data_url = create_qr_data_url(qr_token)

    try:
        # Payment, booking confirmation, and release of TTL fields commit together.
        with database.client.start_session() as session:
            with session.start_transaction():
                transaction_now = datetime.now(UTC)
                booking_update: dict[str, Any] = {
                    "$inc": {"payment_attempts": 1},
                }
                if action == "PAY":
                    booking_update = {
                        "$set": {
                            "status": "CONFIRMED",
                            "confirmed_at": transaction_now,
                            "booking_code": booking_code,
                            "qr_token": qr_token,
                            "qr_code_data_url": qr_code_data_url,
                            "qr_expires_at": qr_expires_at,
                        },
                        "$inc": {"payment_attempts": 1},
                    }
                updated_booking = database.bookings.find_one_and_update(
                    {
                        **booking_filter,
                        "status": "PENDING_PAYMENT",
                        "hold_expires_at": {"$gt": transaction_now},
                    },
                    booking_update,
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                if updated_booking is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="The hold expired or this booking was already processed",
                    )

                if action == "PAY":
                    database.reservations.update_many(
                        {"booking_id": booking_id, "status": "PENDING_PAYMENT"},
                        {
                            "$set": {"status": "CONFIRMED"},
                            "$unset": {"holdExpiresAt": ""},
                        },
                        session=session,
                    )
                database.payments.insert_one(payment, session=session)
    except DuplicateKeyError as error:
        # A concurrent successful request may have committed the one paid record.
        paid_result = find_paid_result(database, booking_filter)
        if paid_result is not None:
            return paid_result
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This booking payment was already processed",
        ) from error
    except OperationFailure as error:
        if error.code == 112 or error.has_error_label("TransientTransactionError"):
            paid_result = find_paid_result(database, booking_filter)
            if paid_result is not None:
                return paid_result
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The booking changed while payment was processing",
            ) from error
        raise
    except HTTPException:
        # If a concurrent pay already won, return its result instead of creating a
        # second payment or incorrectly telling the driver their booking failed.
        paid_result = find_paid_result(database, booking_filter)
        if paid_result is not None:
            return paid_result
        latest_booking = database.bookings.find_one(booking_filter)
        if (
            latest_booking is not None
            and latest_booking.get("status") == "PENDING_PAYMENT"
            and latest_booking.get("hold_expires_at") <= datetime.now(UTC)
        ):
            expire_booking_and_release(database, latest_booking, datetime.now(UTC))
        raise

    if action == "FAIL":
        return {
            "booking_id": str(booking_id),
            "booking_status": "PENDING_PAYMENT",
            "payment_id": str(payment_id),
            "payment_status": "FAILED",
            "payment_method": method,
            "amount": booking["amount"],
            "reference": reference,
            "hold_expires_at": booking["hold_expires_at"].isoformat(),
        }

    return build_paid_response(updated_booking, payment)

