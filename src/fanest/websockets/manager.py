from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._socket_rooms: dict[WebSocket, set[str]] = defaultdict(set)

    def join(self, room: str, websocket: WebSocket) -> None:
        self._rooms[room].add(websocket)
        self._socket_rooms[websocket].add(room)

    def leave(self, room: str, websocket: WebSocket) -> None:
        self._rooms.get(room, set()).discard(websocket)
        if not self._rooms.get(room):
            self._rooms.pop(room, None)
        self._socket_rooms.get(websocket, set()).discard(room)
        if not self._socket_rooms.get(websocket):
            self._socket_rooms.pop(websocket, None)

    def disconnect(self, websocket: WebSocket) -> None:
        for room in list(self._socket_rooms.get(websocket, set())):
            self.leave(room, websocket)

    def rooms(self) -> list[str]:
        return list(self._rooms)

    def connections(self, room: str | None = None) -> list[WebSocket]:
        if room is not None:
            return list(self._rooms.get(room, set()))
        return list(self._socket_rooms)

    async def broadcast(
        self,
        room: str,
        event: str,
        data: Any,
        *,
        exclude: WebSocket | None = None,
    ) -> None:
        for websocket in list(self._rooms.get(room, set())):
            if websocket is exclude:
                continue
            await websocket.send_json({"event": event, "data": data})
