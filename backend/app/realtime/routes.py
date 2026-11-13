"""Authenticated WebSocket endpoint for live lot updates."""

from typing import Any

import jwt
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jwt.exceptions import InvalidTokenError

from app.config import get_settings
from app.db import get_database
from app.realtime.manager import connection_manager


router = APIRouter(tags=["live updates"])
ALLOWED_ROLES = {"admin", "staff", "driver"}


async def close_unauthorized(websocket: WebSocket, code: int, reason: str) -> None:
    """Accept then close so the client receives the application close code."""
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


@router.websocket("/ws/live")
async def live_updates(websocket: WebSocket) -> None:
    """Authenticate a JWT and stream refresh events for an optional lot."""
    token = websocket.query_params.get("access_token")
    if not token:
        await close_unauthorized(websocket, 4401, "Access token required")
        return

    try:
        settings = get_settings()
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id = claims.get("sub")
        if not isinstance(user_id, str):
            raise InvalidTokenError("Token has no user id")
        user_object_id = ObjectId(user_id)
    except (InvalidTokenError, InvalidId):
        await close_unauthorized(websocket, 4401, "Invalid or expired access token")
        return

    database = get_database()
    user = database.users.find_one({"_id": user_object_id, "is_active": True})
    if user is None or user.get("role") not in ALLOWED_ROLES:
        await close_unauthorized(websocket, 4401, "Account is not active")
        return

    requested_lot = websocket.query_params.get("lot_id")
    lot_id: ObjectId | None = None
    if requested_lot:
        try:
            lot_id = ObjectId(requested_lot)
        except InvalidId:
            await close_unauthorized(websocket, 4400, "Invalid lot id")
            return
        if database.lots.find_one({"_id": lot_id}) is None:
            await close_unauthorized(websocket, 4404, "Lot not found")
            return

    await connection_manager.connect(
        websocket,
        user_id=str(user_object_id),
        role=user["role"],
        lot_id=str(lot_id) if lot_id else None,
    )
    try:
        while True:
            # Keep the socket open; changes are sent by the background watcher.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        connection_manager.disconnect(websocket)

