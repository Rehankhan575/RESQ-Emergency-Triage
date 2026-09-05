import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

logger = logging.getLogger("ws_manager")

# Reserved key that subscribes to all sessions
_ALL_KEY = "__all__"


class ConnectionManager:
    """
    Manages WebSocket connections keyed by session_id.

    Listeners subscribed to _ALL_KEY receive every broadcast regardless of
    which session_id produced it.
    """

    def __init__(self):
        # session_id (or "__all__") -> list of live WebSocket connections
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, session_id: str) -> None:
        """Accept the connection and register it under session_id."""
        await websocket.accept()
        self.active_connections.setdefault(session_id, []).append(websocket)
        logger.info(f"WS connected: session={session_id!r}, total={self._count()}")

    def disconnect(self, websocket: WebSocket, session_id: str) -> None:
        """Remove a websocket from the registry. Safe to call even if not found."""
        bucket = self.active_connections.get(session_id, [])
        if websocket in bucket:
            bucket.remove(websocket)
        if not bucket:
            self.active_connections.pop(session_id, None)
        logger.info(f"WS disconnected: session={session_id!r}, remaining={self._count()}")

    async def broadcast(
        self, session_id: str, event_type: str, data: dict
    ) -> None:
        """
        Send a JSON message to:
          - all connections subscribed to `session_id`
          - all connections subscribed to _ALL_KEY

        Dead connections are silently removed.
        """
        payload = {
            "type": event_type,
            "session_id": session_id,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        targets: list[WebSocket] = []
        targets += self.active_connections.get(session_id, []).copy()
        targets += self.active_connections.get(_ALL_KEY, []).copy()

        # De-duplicate if the same socket somehow ends up in both lists
        seen: set[int] = set()
        unique_targets = []
        for ws in targets:
            if id(ws) not in seen:
                seen.add(id(ws))
                unique_targets.append(ws)

        dead: list[tuple[WebSocket, str]] = []
        for ws in unique_targets:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_json(payload)
                else:
                    # Stale connection — schedule for removal
                    key = session_id if ws in self.active_connections.get(session_id, []) else _ALL_KEY
                    dead.append((ws, key))
            except Exception as exc:
                logger.warning(f"Failed to send to WS ({event_type}): {exc}")
                key = session_id if ws in self.active_connections.get(session_id, []) else _ALL_KEY
                dead.append((ws, key))

        for ws, key in dead:
            self.disconnect(ws, key)

    def _count(self) -> int:
        return sum(len(v) for v in self.active_connections.values())


# Module-level singleton — matches the style used in voice_agent.py
ws_manager = ConnectionManager()
