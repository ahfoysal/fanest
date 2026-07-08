from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._socket_rooms: dict[WebSocket, set[str]] = defaultdict(set)
        self._connections: set[WebSocket] = set()

    def connect(self, websocket: WebSocket) -> None:
        self._connections.add(websocket)

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
        self._socket_rooms.pop(websocket, None)
        self._connections.discard(websocket)

    def rooms(self) -> list[str]:
        return list(self._rooms)

    def connections(self, room: str | None = None) -> list[WebSocket]:
        if room is not None:
            return list(self._rooms.get(room, set()))
        return list(self._connections)

    async def broadcast_all(
        self,
        event: str,
        data: Any,
        *,
        exclude: WebSocket | None = None,
    ) -> None:
        for websocket in list(self._connections):
            if websocket is exclude:
                continue
            await websocket.send_json({"event": event, "data": data})

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


class SocketIoRoomEmitter:
    def __init__(self, manager: WebSocketManager, room: str):
        self.manager = manager
        self.room = room

    async def emit(self, event: str, data: Any, *, exclude: WebSocket | None = None) -> None:
        await self.manager.broadcast(self.room, event, data, exclude=exclude)


class SocketIoServer:
    def __init__(self, manager: WebSocketManager):
        self.manager = manager

    def join(self, websocket: WebSocket, room: str) -> None:
        self.manager.join(room, websocket)

    def leave(self, websocket: WebSocket, room: str) -> None:
        self.manager.leave(room, websocket)

    def to(self, room: str) -> SocketIoRoomEmitter:
        return SocketIoRoomEmitter(self.manager, room)

    async def emit(self, *args: Any, exclude: WebSocket | None = None) -> None:
        if len(args) == 3:
            websocket, event, data = args
            await websocket.send_json({"event": event, "data": data})
            return
        if len(args) == 2:
            event, data = args
            await self.manager.broadcast_all(event, data, exclude=exclude)
            return
        raise TypeError("emit expects (websocket, event, data) or (event, data)")
