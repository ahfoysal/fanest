import base64
import inspect
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect


class UnsupportedSocketIoProtocolError(RuntimeError):
    pass


class WsException(Exception):
    def __init__(self, error: Any = "WebSocket error"):
        self.error = error
        super().__init__(str(error))

    def get_error(self) -> Any:
        return self.error


@dataclass(frozen=True)
class WsResponse:
    event: str
    data: Any


SocketListener = Callable[..., Any]


class WebSocketManager:
    def __init__(self) -> None:
        self._rooms: dict[tuple[str, str], set[WebSocket]] = defaultdict(set)
        self._socket_rooms: dict[WebSocket, set[tuple[str, str]]] = defaultdict(set)
        self._namespaces: dict[str, set[WebSocket]] = defaultdict(set)
        self._socket_namespaces: dict[WebSocket, set[str]] = defaultdict(set)
        self._connections: set[WebSocket] = set()
        self._listeners: dict[str, list[SocketListener]] = defaultdict(list)

    def connect(self, websocket: WebSocket) -> None:
        self._connections.add(websocket)
        self.join_namespace("/", websocket)

    def join(self, room: str, websocket: WebSocket, *, namespace: str = "/") -> None:
        namespace = self._normalize_namespace(namespace)
        key = (namespace, room)
        self.join_namespace(namespace, websocket)
        self._rooms[key].add(websocket)
        self._socket_rooms[websocket].add(key)

    def leave(self, room: str, websocket: WebSocket, *, namespace: str = "/") -> None:
        namespace = self._normalize_namespace(namespace)
        key = (namespace, room)
        self._rooms.get(key, set()).discard(websocket)
        if not self._rooms.get(key):
            self._rooms.pop(key, None)
        self._socket_rooms.get(websocket, set()).discard(key)
        if not self._socket_rooms.get(websocket):
            self._socket_rooms.pop(websocket, None)

    def disconnect(self, websocket: WebSocket) -> None:
        for namespace, room in list(self._socket_rooms.get(websocket, set())):
            self.leave(room, websocket, namespace=namespace)
        for namespace in list(self._socket_namespaces.get(websocket, set())):
            self.leave_namespace(namespace, websocket)
        self._socket_rooms.pop(websocket, None)
        self._socket_namespaces.pop(websocket, None)
        self._connections.discard(websocket)

    def join_namespace(self, namespace: str, websocket: WebSocket) -> None:
        namespace = self._normalize_namespace(namespace)
        self._namespaces[namespace].add(websocket)
        self._socket_namespaces[websocket].add(namespace)

    def leave_namespace(self, namespace: str, websocket: WebSocket) -> None:
        namespace = self._normalize_namespace(namespace)
        # Leaving a namespace also removes the socket from every room scoped to
        # it, otherwise room broadcasts within that namespace would still reach
        # the departed socket (socket.io parity).
        for room_namespace, room in list(self._socket_rooms.get(websocket, set())):
            if room_namespace == namespace:
                self.leave(room, websocket, namespace=namespace)
        self._namespaces.get(namespace, set()).discard(websocket)
        if not self._namespaces.get(namespace):
            self._namespaces.pop(namespace, None)
        self._socket_namespaces.get(websocket, set()).discard(namespace)
        if not self._socket_namespaces.get(websocket):
            self._socket_namespaces.pop(websocket, None)

    def namespaces(self) -> list[str]:
        return list(self._namespaces)

    def rooms(self, namespace: str | None = None) -> list[str]:
        if namespace is None:
            return list(dict.fromkeys(room for _, room in self._rooms))
        normalized = self._normalize_namespace(namespace)
        return [room for room_namespace, room in self._rooms if room_namespace == normalized]

    def connections(self, room: str | None = None, *, namespace: str | None = None) -> list[WebSocket]:
        if room is not None:
            if namespace is None:
                sockets: set[WebSocket] = set()
                for (_, scoped_room), room_sockets in self._rooms.items():
                    if scoped_room == room:
                        sockets.update(room_sockets)
                return list(sockets)
            return list(self._rooms.get((self._normalize_namespace(namespace), room), set()))
        if namespace is not None:
            return list(self._namespaces.get(self._normalize_namespace(namespace), set()))
        return list(self._connections)

    def namespace_connections(self, namespace: str) -> list[WebSocket]:
        return list(self._namespaces.get(self._normalize_namespace(namespace), set()))

    def in_namespace(self, websocket: WebSocket, namespace: str) -> bool:
        return self._normalize_namespace(namespace) in self._socket_namespaces.get(websocket, set())

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
            if not await self._send_json(websocket, {"event": event, "data": data}):
                self.disconnect(websocket)

    async def broadcast(
        self,
        room: str,
        event: str,
        data: Any,
        *,
        exclude: WebSocket | None = None,
        namespace: str = "/",
    ) -> None:
        key = (self._normalize_namespace(namespace), room)
        for websocket in list(self._rooms.get(key, set())):
            if websocket is exclude:
                continue
            payload = {"event": event, "data": data}
            if key[0] != "/":
                payload["namespace"] = key[0]
            if not await self._send_json(websocket, payload):
                self.disconnect(websocket)

    async def broadcast_namespace(
        self,
        namespace: str,
        event: str,
        data: Any,
        *,
        exclude: WebSocket | None = None,
    ) -> None:
        for websocket in list(self._namespaces.get(self._normalize_namespace(namespace), set())):
            if websocket is exclude:
                continue
            normalized = self._normalize_namespace(namespace)
            payload = {"event": event, "data": data}
            if normalized != "/":
                payload["namespace"] = normalized
            if not await self._send_json(websocket, payload):
                self.disconnect(websocket)

    async def _send_json(self, websocket: WebSocket, payload: dict[str, Any]) -> bool:
        try:
            await websocket.send_json(self._json_safe(payload))
            return True
        except (RuntimeError, WebSocketDisconnect):
            return False

    def on(self, event: str, listener: SocketListener) -> SocketListener:
        self._listeners[event].append(listener)
        return listener

    async def emit_lifecycle(self, event: str, websocket: WebSocket, namespace: str = "/") -> None:
        for listener in list(self._listeners.get(event, [])):
            try:
                parameter_count = len(inspect.signature(listener).parameters)
            except (TypeError, ValueError):
                parameter_count = 2
            result = listener(websocket) if parameter_count == 1 else listener(websocket, namespace)
            if inspect.isawaitable(result):
                await result

    def _normalize_namespace(self, namespace: str) -> str:
        stripped = namespace.strip() or "/"
        return stripped if stripped.startswith("/") else f"/{stripped}"

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, bytes):
            return {
                "__fanest_binary__": base64.b64encode(value).decode("ascii"),
                "encoding": "base64",
            }
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self._json_safe(item) for item in value]
        return value


class SocketIoRoomEmitter:
    def __init__(self, manager: WebSocketManager, room: str, namespace: str = "/"):
        self.manager = manager
        self.room = room
        self.namespace = manager._normalize_namespace(namespace)

    async def emit(self, event: str, data: Any, *, exclude: WebSocket | None = None) -> None:
        await self.manager.broadcast(self.room, event, data, exclude=exclude, namespace=self.namespace)


class SocketIoServer:
    def __init__(self, manager: WebSocketManager):
        self.manager = manager

    def join(self, websocket: WebSocket, room: str, *, namespace: str = "/") -> None:
        self.manager.join(room, websocket, namespace=namespace)

    def leave(self, websocket: WebSocket, room: str, *, namespace: str = "/") -> None:
        self.manager.leave(room, websocket, namespace=namespace)

    def to(self, room: str) -> SocketIoRoomEmitter:
        return SocketIoRoomEmitter(self.manager, room)

    def of(self, namespace: str) -> "SocketIoNamespace":
        return SocketIoNamespace(self.manager, namespace)

    def on(self, event: str, listener: SocketListener) -> SocketListener:
        return self.manager.on(event, listener)

    async def emit(self, *args: Any, exclude: WebSocket | None = None) -> None:
        if len(args) == 3:
            websocket, event, data = args
            if not await self.manager._send_json(websocket, {"event": event, "data": data}):
                self.manager.disconnect(websocket)
            return
        if len(args) == 2:
            event, data = args
            await self.manager.broadcast_all(event, data, exclude=exclude)
            return
        raise TypeError("emit expects (websocket, event, data) or (event, data)")


class SocketIoNamespace:
    def __init__(self, manager: WebSocketManager, namespace: str):
        self.manager = manager
        self.namespace = manager._normalize_namespace(namespace)

    def join(self, websocket: WebSocket) -> None:
        self.manager.join_namespace(self.namespace, websocket)

    def leave(self, websocket: WebSocket) -> None:
        self.manager.leave_namespace(self.namespace, websocket)

    async def emit(self, event: str, data: Any, *, exclude: WebSocket | None = None) -> None:
        await self.manager.broadcast_namespace(self.namespace, event, data, exclude=exclude)

    def join_room(self, websocket: WebSocket, room: str) -> None:
        self.manager.join(room, websocket, namespace=self.namespace)

    def leave_room(self, websocket: WebSocket, room: str) -> None:
        self.manager.leave(room, websocket, namespace=self.namespace)

    def to(self, room: str) -> SocketIoRoomEmitter:
        return SocketIoRoomEmitter(self.manager, room, namespace=self.namespace)
