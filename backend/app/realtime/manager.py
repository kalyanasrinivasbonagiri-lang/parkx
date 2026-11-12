"""Manage authenticated WebSocket clients and scoped live events."""

from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect


@dataclass
class LiveConnection:
    """Identity and lot scope associated with one live socket."""

    websocket: WebSocket
    user_id: str
    role: str
    lot_id: str | None


class ConnectionManager:
    """Keep active sockets and broadcast only event metadata."""

    def __init__(self) -> None:
        self._connections: list[LiveConnection] = []

    async def connect(
        self,
        websocket: WebSocket,
        user_id: str,
        role: str,
        lot_id: str | None,
    ) -> None:
        """Accept and remember an already-authenticated socket."""
        await websocket.accept()
        self._connections.append(LiveConnection(websocket, user_id, role, lot_id))

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a socket after its client disconnects."""
        self._connections = [
            connection
            for connection in self._connections
            if connection.websocket is not websocket
        ]

    async def broadcast(self, event: dict[str, str | None]) -> None:
        """Send event metadata to matching users without private document fields."""
        target_user_id = event.get("target_user_id")
        event_lot_id = event.get("lot_id")
        disconnected: list[WebSocket] = []

        for connection in list(self._connections):
            if target_user_id and connection.user_id != target_user_id:
                continue
            if (
                connection.role in {"staff", "driver"}
                and connection.lot_id
                and event_lot_id
                and connection.lot_id != event_lot_id
            ):
                continue

            safe_event = {
                key: value
                for key, value in event.items()
                if key != "target_user_id"
            }
            try:
                await connection.websocket.send_json(safe_event)
            except Exception:
                disconnected.append(connection.websocket)

        for websocket in disconnected:
            self.disconnect(websocket)


connection_manager = ConnectionManager()

