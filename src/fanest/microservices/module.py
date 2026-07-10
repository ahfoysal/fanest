import asyncio
import inspect
import json
import logging
import subprocess
import sys
import warnings
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

from fanest.core.container import FaNestContainer
from fanest.core.metadata import ExecutionContext, ValueProvider
from fanest.core.scanner import ModuleScanner
from fanest import Inject, Module, use_value


_UNHANDLED = object()

logger = logging.getLogger("fanest.microservices")


@dataclass(frozen=True)
class MicroserviceContext:
    pattern: Any
    data: Any
    transport: str = "memory"
    raw_pattern: str | None = None
    headers: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    message: Any | None = None
    correlation_id: str | None = None
    reply_to: str | None = None


@dataclass(frozen=True)
class TcpContext(MicroserviceContext):
    remote_address: str | None = None
    remote_port: int | None = None


@dataclass(frozen=True)
class NatsContext(MicroserviceContext):
    subject: str | None = None


@dataclass(frozen=True)
class RmqContext(MicroserviceContext):
    routing_key: str | None = None
    exchange: str | None = None


@dataclass(frozen=True)
class KafkaContext(MicroserviceContext):
    topic: str | None = None
    partition: int | None = None
    offset: int | None = None


@dataclass(frozen=True)
class GrpcContext(MicroserviceContext):
    method: str | None = None


@dataclass(frozen=True)
class MqttContext(MicroserviceContext):
    topic: str | None = None


class Transport(str, Enum):
    MEMORY = "memory"
    TCP = "tcp"
    REDIS = "redis"
    NATS = "nats"
    RABBITMQ = "rabbitmq"
    KAFKA = "kafka"
    GRPC = "grpc"
    MQTT = "mqtt"
    CUSTOM = "custom"


class MicroservicePatternError(KeyError):
    pass


class MicroserviceDuplicateHandlerError(ValueError):
    pass


class MicroserviceRemoteError(RuntimeError):
    def __init__(self, message: str, *, error_type: str = "Error") -> None:
        super().__init__(message)
        self.error_type = error_type


class MicroserviceEventError(RuntimeError):
    def __init__(self, pattern: Any, errors: list[BaseException]) -> None:
        self.pattern = pattern
        self.errors = errors
        super().__init__(f"{len(errors)} event handler(s) failed for pattern: {pattern!r}")


class MicroserviceTimeoutError(TimeoutError):
    pass


class MicroserviceTransportError(RuntimeError):
    pass


class MicroserviceSerializer(Protocol):
    def serialize(self, value: Any) -> bytes: ...

    def deserialize(self, value: bytes | str | None) -> Any: ...


class JsonMicroserviceSerializer:
    def serialize(self, value: Any) -> bytes:
        return json.dumps(value, default=str).encode()

    def deserialize(self, value: bytes | str | None) -> Any:
        if value is None or value in (b"", ""):
            return None
        if isinstance(value, bytes):
            value = value.decode()
        return json.loads(value)


@dataclass(frozen=True)
class BrokerMessage:
    pattern: Any
    data: Any
    headers: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any | None = None
    correlation_id: str | None = None
    reply_to: str | None = None


@dataclass(frozen=True)
class ClientProxyOptions:
    timeout: float | None = None
    retries: int = 0
    retry_delay: float = 0


@runtime_checkable
class TransportAdapter(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def send(self, pattern: Any, data: Any, **kwargs: Any) -> Any: ...

    async def emit(self, pattern: Any, data: Any, **kwargs: Any) -> None: ...


@runtime_checkable
class ServerTransportAdapter(TransportAdapter, Protocol):
    async def listen(
        self,
        on_message: Callable[[BrokerMessage], Any],
        on_event: Callable[[BrokerMessage], Any],
    ) -> None: ...


def serialize_pattern(pattern: Any) -> str:
    if isinstance(pattern, str):
        return pattern
    try:
        return json.dumps(pattern, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        return str(pattern)


def _decode_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def MessagePattern(pattern: Any):
    def decorator(handler):
        setattr(handler, "__fanest_message_pattern__", pattern)
        return handler

    return decorator


def EventPattern(pattern: Any):
    def decorator(handler):
        setattr(handler, "__fanest_event_pattern__", pattern)
        return handler

    return decorator


class InMemoryTransport:
    context_type: type[MicroserviceContext] = MicroserviceContext

    def __init__(
        self,
        name: str = "memory",
        *,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        self.name = name
        self.serializer = serializer or JsonMicroserviceSerializer()
        self.deserializer = deserializer or self.serializer
        self.message_handlers: dict[str, Any] = {}
        self.message_patterns: dict[str, Any] = {}
        self.event_handlers: dict[str, list[Any]] = {}
        self.event_patterns: dict[str, Any] = {}
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    def register_message(self, pattern: Any, handler: Any) -> None:
        key = serialize_pattern(pattern)
        existing = self.message_handlers.get(key)
        if existing is not None and self._handler_key(existing) != self._handler_key(handler):
            raise MicroserviceDuplicateHandlerError(f"Duplicate message handler registered for pattern: {pattern!r}")
        self.message_handlers[key] = handler
        self.message_patterns[key] = pattern

    def register_event(self, pattern: Any, handler: Any) -> None:
        key = serialize_pattern(pattern)
        handlers = self.event_handlers.setdefault(key, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            handlers.append(handler)
        self.event_patterns[key] = pattern

    async def send(self, pattern: Any, data: Any) -> Any:
        return await self._dispatch_message(pattern, data)

    async def _dispatch_message(
        self,
        pattern: Any,
        data: Any,
        *,
        headers: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        raw: Any | None = None,
        correlation_id: str | None = None,
        reply_to: str | None = None,
    ) -> Any:
        key = serialize_pattern(pattern)
        if key not in self.message_handlers:
            raise MicroservicePatternError(f"No message handler registered for pattern: {pattern}")
        handler = self.message_handlers[key]
        context = self.create_context(
            pattern=self.message_patterns[key],
            data=data,
            raw_pattern=key,
            headers=headers,
            metadata=metadata,
            message=raw,
            correlation_id=correlation_id,
            reply_to=reply_to,
        )
        result = handler(data, context)
        if inspect.isawaitable(result):
            return await result
        return result

    async def emit(self, pattern: Any, data: Any) -> None:
        await self._dispatch_event(pattern, data)

    async def _dispatch_event(
        self,
        pattern: Any,
        data: Any,
        *,
        headers: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        raw: Any | None = None,
        correlation_id: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        key = serialize_pattern(pattern)
        errors: list[BaseException] = []
        for handler in self.event_handlers.get(key, []):
            try:
                context = self.create_context(
                    pattern=self.event_patterns[key],
                    data=data,
                    raw_pattern=key,
                    headers=headers,
                    metadata=metadata,
                    message=raw,
                    correlation_id=correlation_id,
                    reply_to=reply_to,
                )
                result = handler(data, context)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise MicroserviceEventError(pattern, errors)

    def _handler_key(self, handler: Any) -> Any:
        return getattr(handler, "__fanest_registration_key__", handler)

    def create_context(
        self,
        *,
        pattern: Any,
        data: Any,
        raw_pattern: str | None = None,
        headers: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        message: Any | None = None,
        correlation_id: str | None = None,
        reply_to: str | None = None,
    ) -> MicroserviceContext:
        return self.context_type(
            pattern=pattern,
            data=data,
            transport=self.name,
            raw_pattern=raw_pattern,
            headers=headers or {},
            metadata=metadata or {},
            message=message,
            correlation_id=correlation_id,
            reply_to=reply_to,
        )

    def _dump(self, value: Any) -> str:
        return self.serializer.serialize(value).decode()

    def _load(self, value: bytes | str | None) -> Any:
        return self.deserializer.deserialize(value)


class RedisTransport(InMemoryTransport):
    context_type = MicroserviceContext

    def __init__(
        self,
        *,
        url: str | None = None,
        prefix: str = "fanest:microservice:",
        response_timeout: float = 5,
        client: Any | None = None,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__("redis", serializer=serializer, deserializer=deserializer)
        self.url = url
        self.prefix = prefix
        self.response_timeout = response_timeout
        self._client: Any | None = None
        if client is not None:
            self._client = client
            return
        if url is not None:
            try:
                import redis.asyncio as redis  # type: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover - exercised without redis installed
                raise ImportError(
                    "RedisTransport requires the 'redis' package. "
                    "Install it with: pip install 'fanest[redis]'"
                ) from exc
            self._client = redis.Redis.from_url(url)

    async def connect(self) -> None:
        if self._client is not None:
            await self._client.ping()
        await super().connect()

    async def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        await super().close()

    async def send(self, pattern: Any, data: Any) -> Any:
        pattern_key = serialize_pattern(pattern)
        if pattern_key in self.message_handlers or self._client is None:
            return await super().send(pattern, data)
        request_id = str(uuid4())
        reply_stream = self._stream(f"reply:{request_id}")
        await self._client.xadd(
            self._stream("requests"),
            {
                "id": request_id,
                "pattern": pattern_key,
                "data": self._dump(data),
                "reply_to": reply_stream,
                "headers": self._dump({}),
            },
        )
        response = await self._client.xread({reply_stream: "0-0"}, block=int(self.response_timeout * 1000), count=1)
        await self._client.delete(reply_stream)
        if not response:
            raise TimeoutError(f"No Redis microservice response for pattern: {pattern}")
        _, messages = response[0]
        _, fields = messages[0]
        payload = self._decode_fields(fields)
        if payload.get("error"):
            raise MicroserviceRemoteError(payload["error"], error_type=payload.get("error_type") or "Error")
        return self._load(payload.get("data", "null"))

    async def emit(self, pattern: Any, data: Any) -> None:
        pattern_key = serialize_pattern(pattern)
        if self.event_handlers.get(pattern_key) or self._client is None:
            await super().emit(pattern, data)
            return
        await self._client.xadd(
            self._stream("events"),
            {"pattern": pattern_key, "data": self._dump(data), "headers": self._dump({})},
        )

    async def listen_once(self, *, last_request_id: str = "0-0", last_event_id: str = "0-0") -> tuple[str, str]:
        if self._client is None:
            return last_request_id, last_event_id
        streams = {
            self._stream("requests"): last_request_id,
            self._stream("events"): last_event_id,
        }
        response = await self._client.xread(streams, block=100, count=1)
        for stream, messages in response:
            stream_name = self._decode(stream)
            for message_id, fields in messages:
                decoded_id = self._decode(message_id)
                payload = self._decode_fields(fields)
                if stream_name.endswith(":requests"):
                    await self._handle_request(decoded_id, payload)
                    last_request_id = decoded_id
                elif stream_name.endswith(":events"):
                    await self._handle_event(payload)
                    last_event_id = decoded_id
        return last_request_id, last_event_id

    async def listen_forever(self) -> None:
        last_request_id = "$"
        last_event_id = "$"
        while True:
            last_request_id, last_event_id = await self.listen_once(
                last_request_id=last_request_id,
                last_event_id=last_event_id,
            )

    async def _handle_request(self, message_id: str, payload: dict[str, str]) -> None:
        pattern = payload.get("pattern", "")
        reply_to = payload.get("reply_to", "")
        try:
            result = await self._dispatch_message(
                pattern,
                self._load(payload.get("data", "null")),
                headers=self._load(payload.get("headers", "{}")) or {},
                raw=payload,
                correlation_id=payload.get("id") or message_id,
                reply_to=reply_to,
            )
            response = {"id": message_id, "data": self._dump(result), "error": "", "error_type": ""}
        except Exception as exc:
            response = {"id": message_id, "data": "null", "error": str(exc), "error_type": type(exc).__name__}
        if reply_to and self._client is not None:
            await self._client.xadd(reply_to, response)

    async def _handle_event(self, payload: dict[str, str]) -> None:
        pattern = payload.get("pattern", "")
        try:
            await self._dispatch_event(
                pattern,
                self._load(payload.get("data", "null")),
                headers=self._load(payload.get("headers", "{}")) or {},
                raw=payload,
            )
        except Exception:
            logger.exception(
                "Unhandled error in event handler(s) for pattern %r; listener continuing.",
                pattern,
            )

    def _stream(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def _decode_fields(self, fields: dict[Any, Any]) -> dict[str, str]:
        return {self._decode(key): self._decode(value) for key, value in fields.items()}

    def _decode(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return str(value)


class TcpTransport(InMemoryTransport):
    context_type = TcpContext

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8877,
        bind_host: str | None = None,
        response_timeout: float = 5,
        max_frame_bytes: int = 16 * 1024 * 1024,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__("tcp", serializer=serializer, deserializer=deserializer)
        self.host = host
        self.port = port
        self.bind_host = bind_host or host
        self.response_timeout = response_timeout
        self.max_frame_bytes = max_frame_bytes
        self._server: asyncio.AbstractServer | None = None

    async def connect(self) -> None:
        if (self.message_handlers or self.event_handlers) and self._server is None:
            self._server = await asyncio.start_server(
                self._handle_client, self.bind_host, self.port, limit=self.max_frame_bytes
            )
            sockets = self._server.sockets or []
            if sockets:
                bound_host, bound_port = sockets[0].getsockname()[:2]
                self.bind_host = str(bound_host)
                self.port = int(bound_port)
        await super().connect()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await super().close()

    async def send(self, pattern: Any, data: Any) -> Any:
        if serialize_pattern(pattern) in self.message_handlers:
            return await super().send(pattern, data)
        response = await self._round_trip(
            {
                "__fanest_ms__": True,
                "type": "request",
                "id": str(uuid4()),
                "pattern": serialize_pattern(pattern),
                "data": data,
                "headers": {},
            }
        )
        if response.get("error"):
            raise MicroserviceRemoteError(
                str(response["error"]),
                error_type=str(response.get("error_type") or "Error"),
            )
        return response.get("data")

    async def emit(self, pattern: Any, data: Any) -> None:
        if self.event_handlers.get(serialize_pattern(pattern)):
            await super().emit(pattern, data)
            return
        await self._write_frame(
            {
                "__fanest_ms__": True,
                "type": "event",
                "id": str(uuid4()),
                "pattern": serialize_pattern(pattern),
                "data": data,
                "headers": {},
            },
            wait_for_response=False,
        )

    async def _round_trip(self, frame: dict[str, Any]) -> dict[str, Any]:
        response = await self._write_frame(frame, wait_for_response=True)
        if not isinstance(response, dict):
            raise MicroserviceTransportError("TCP microservice response must be an object envelope")
        return response

    async def _write_frame(self, frame: dict[str, Any], *, wait_for_response: bool) -> Any:
        try:
            reader, writer = await asyncio.open_connection(
                self.host, self.port, limit=self.max_frame_bytes
            )
        except OSError as exc:
            raise MicroserviceTransportError(
                f"Could not connect to TCP microservice at {self.host}:{self.port}"
            ) from exc
        try:
            writer.write(self.serializer.serialize(frame) + b"\n")
            await writer.drain()
            if not wait_for_response:
                return None
            line = await asyncio.wait_for(reader.readline(), timeout=self.response_timeout)
            if not line:
                raise MicroserviceTransportError("TCP microservice closed before sending a response")
            return self._load(line.rstrip(b"\r\n"))
        except asyncio.TimeoutError as exc:
            raise MicroserviceTimeoutError(
                f"No TCP microservice response for pattern: {frame.get('pattern')!r}"
            ) from exc
        finally:
            writer.close()
            with suppress(OSError, RuntimeError):
                await writer.wait_closed()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            line = await reader.readline()
            if not line:
                return
            frame = self._load(line.rstrip(b"\r\n"))
            if not isinstance(frame, dict):
                raise MicroserviceTransportError("TCP microservice frame must be an object envelope")
            frame_type = frame.get("type")
            if frame_type == "request":
                response = await self._handle_request_frame(frame, peer)
                writer.write(self.serializer.serialize(response) + b"\n")
                await writer.drain()
            elif frame_type == "event":
                await self._handle_event_frame(frame, peer)
            else:
                raise MicroserviceTransportError(f"Unsupported TCP microservice frame type: {frame_type!r}")
        except Exception as exc:
            error = {
                "__fanest_ms__": True,
                "id": None,
                "data": None,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            with suppress(OSError, RuntimeError):
                writer.write(self.serializer.serialize(error) + b"\n")
                await writer.drain()
        finally:
            writer.close()
            with suppress(OSError, RuntimeError):
                await writer.wait_closed()

    async def _handle_request_frame(self, frame: dict[str, Any], peer: Any) -> dict[str, Any]:
        correlation_id = str(frame.get("id") or "")
        try:
            result = await self._dispatch_message(
                frame.get("pattern", ""),
                frame.get("data"),
                headers=dict(frame.get("headers") or {}),
                metadata=self._peer_metadata(peer),
                raw=frame,
                correlation_id=correlation_id,
                reply_to="tcp",
            )
            return {
                "__fanest_ms__": True,
                "id": correlation_id,
                "data": result,
                "error": "",
                "error_type": "",
            }
        except Exception as exc:
            return {
                "__fanest_ms__": True,
                "id": correlation_id,
                "data": None,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }

    async def _handle_event_frame(self, frame: dict[str, Any], peer: Any) -> None:
        await self._dispatch_event(
            frame.get("pattern", ""),
            frame.get("data"),
            headers=dict(frame.get("headers") or {}),
            metadata=self._peer_metadata(peer),
            raw=frame,
            correlation_id=frame.get("id"),
        )

    def _peer_metadata(self, peer: Any) -> dict[str, Any]:
        if isinstance(peer, tuple) and len(peer) >= 2:
            return {"remote_address": str(peer[0]), "remote_port": int(peer[1])}
        return {}

    def create_context(self, **kwargs: Any) -> MicroserviceContext:
        metadata = kwargs.get("metadata") or {}
        kwargs.setdefault("transport", self.name)
        kwargs["headers"] = kwargs.get("headers") or {}
        kwargs["metadata"] = metadata
        return TcpContext(
            **kwargs,
            remote_address=metadata.get("remote_address"),
            remote_port=metadata.get("remote_port"),
        )


class _BrokerTransport(InMemoryTransport):
    _broker = "network"
    context_type: type[MicroserviceContext] = MicroserviceContext
    _install_hint = "Install the optional broker package for this transport."

    def __init__(
        self,
        *,
        adapter: TransportAdapter | None = None,
        configured: bool = False,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__(self._broker, serializer=serializer, deserializer=deserializer)
        self.adapter = adapter
        if adapter is None and not configured:
            warnings.warn(
                f"The '{self._broker}' microservice transport is running in single-process mode. "
                "Pass a real broker URL/client or adapter=... for cross-service messaging.",
                stacklevel=2,
            )

    async def connect(self) -> None:
        if self.adapter is not None:
            await self.adapter.connect()
        await super().connect()

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        await super().close()

    async def send(self, pattern: Any, data: Any) -> Any:
        if serialize_pattern(pattern) in self.message_handlers or self.adapter is None:
            return await super().send(pattern, data)
        return await self.adapter.send(pattern, data)

    async def emit(self, pattern: Any, data: Any) -> None:
        if self.event_handlers.get(serialize_pattern(pattern)) or self.adapter is None:
            await super().emit(pattern, data)
            return
        await self.adapter.emit(pattern, data)


class NatsTransport(_BrokerTransport):
    _broker = "nats"
    context_type = NatsContext
    _install_hint = "NatsTransport requires nats-py. Install it with: pip install nats-py"

    def __init__(
        self,
        *,
        url: str | None = None,
        subject_prefix: str = "",
        listen_subject: str = ">",
        client: Any | None = None,
        adapter: TransportAdapter | None = None,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__(
            adapter=adapter,
            configured=url is not None or client is not None,
            serializer=serializer,
            deserializer=deserializer,
        )
        self.url = url
        self.subject_prefix = subject_prefix
        self.listen_subject = listen_subject
        self._client = client
        self._subscription: Any | None = None

    async def connect(self) -> None:
        if self.adapter is not None:
            await self.adapter.connect()
        if self._client is None and self.url is not None:
            try:
                import nats  # type: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover - depends on optional package
                raise ImportError(self._install_hint) from exc
            self._client = await nats.connect(self.url)
        self.connected = True

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        if self._subscription is not None:
            unsubscribe = getattr(self._subscription, "unsubscribe", None)
            if unsubscribe is not None:
                result = unsubscribe()
                if inspect.isawaitable(result):
                    await result
            self._subscription = None
        if self._client is not None:
            drain = getattr(self._client, "drain", None)
            close = getattr(self._client, "close", None)
            if drain is not None:
                result = drain()
                if inspect.isawaitable(result):
                    await result
            elif close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        self.connected = False

    async def send(self, pattern: Any, data: Any) -> Any:
        if serialize_pattern(pattern) in self.message_handlers or self._client is None:
            return await super().send(pattern, data)
        subject = self._subject(pattern)
        response = await self._client.request(subject, self.serializer.serialize(data))
        payload = self._load(cast(bytes | str | None, getattr(response, "data", response)))
        if isinstance(payload, dict) and payload.get("error"):
            raise MicroserviceRemoteError(
                str(payload["error"]),
                error_type=str(payload.get("error_type") or "Error"),
            )
        return payload

    async def emit(self, pattern: Any, data: Any) -> None:
        if self.event_handlers.get(serialize_pattern(pattern)) or self._client is None:
            await super().emit(pattern, data)
            return
        await self._client.publish(self._subject(pattern), self.serializer.serialize(data))

    async def listen_forever(self) -> None:
        if self._client is None:
            while True:
                await asyncio.sleep(3600)
        client = self._client
        self._subscription = await client.subscribe(self.listen_subject, cb=self._handle_nats_message)
        while True:
            await asyncio.sleep(3600)

    async def _handle_nats_message(self, message: Any) -> None:
        subject = str(getattr(message, "subject", ""))
        reply = getattr(message, "reply", None) or None
        data = self._load(getattr(message, "data", None))
        pattern = self._pattern_from_subject(subject)
        if reply:
            if self._client is None:
                return
            client = self._client
            try:
                result = await self._dispatch_message(
                    pattern,
                    data,
                    raw=message,
                    metadata={"subject": subject},
                    reply_to=reply,
                )
                await client.publish(reply, self.serializer.serialize(result))
            except Exception as exc:
                await client.publish(
                    reply,
                    self.serializer.serialize({"error": str(exc), "error_type": type(exc).__name__}),
                )
            return
        try:
            await self._dispatch_event(pattern, data, raw=message, metadata={"subject": subject})
        except Exception:
            logger.exception(
                "Unhandled error in event handler(s) for pattern %r; listener continuing.",
                pattern,
            )

    def _subject(self, pattern: Any) -> str:
        return f"{self.subject_prefix}{serialize_pattern(pattern)}"

    def _pattern_from_subject(self, subject: str) -> str:
        if self.subject_prefix and subject.startswith(self.subject_prefix):
            return subject[len(self.subject_prefix) :]
        return subject

    def create_context(self, **kwargs: Any) -> MicroserviceContext:
        metadata = kwargs.get("metadata") or {}
        kwargs.setdefault("transport", self.name)
        kwargs["headers"] = kwargs.get("headers") or {}
        kwargs["metadata"] = metadata
        return NatsContext(**kwargs, subject=metadata.get("subject"))


class RabbitMqTransport(_BrokerTransport):
    _broker = "rabbitmq"
    context_type = RmqContext
    _install_hint = "RabbitMqTransport requires aio-pika. Install it with: pip install aio-pika"

    def __init__(
        self,
        *,
        url: str | None = None,
        exchange: str = "",
        queue: str | None = None,
        routing_key: str = "#",
        client: Any | None = None,
        adapter: TransportAdapter | None = None,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__(
            adapter=adapter,
            configured=url is not None or client is not None,
            serializer=serializer,
            deserializer=deserializer,
        )
        self.url = url
        self.exchange_name = exchange
        self.queue_name = queue
        self.routing_key = routing_key
        self._connection = client
        self._channel: Any | None = None
        self._exchange: Any | None = None
        self._queue: Any | None = None

    async def connect(self) -> None:
        if self.adapter is not None:
            await self.adapter.connect()
        if self._connection is None and self.url is not None:
            try:
                import aio_pika  # type: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover
                raise ImportError(self._install_hint) from exc
            self._connection = await aio_pika.connect_robust(self.url)
        if self._connection is not None and self._channel is None:
            self._channel = await self._connection.channel()
            channel = self._channel
            assert channel is not None
            self._exchange = (
                await channel.declare_exchange(self.exchange_name, durable=True)
                if self.exchange_name
                else channel.default_exchange
            )
        self.connected = True

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        if self._connection is not None:
            close = getattr(self._connection, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        self.connected = False

    async def send(self, pattern: Any, data: Any) -> Any:
        if (
            serialize_pattern(pattern) in self.message_handlers
            or self._exchange is None
            or self._channel is None
        ):
            return await super().send(pattern, data)
        channel = self._channel
        exchange = self._exchange
        correlation_id = str(uuid4())
        callback_queue = await channel.declare_queue(exclusive=True)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        async def on_response(message: Any) -> None:
            if getattr(message, "correlation_id", None) == correlation_id and not future.done():
                future.set_result(self._load(getattr(message, "body", None)))

        await callback_queue.consume(on_response)
        try:
            import aio_pika  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(self._install_hint) from exc
        message = aio_pika.Message(
            body=self.serializer.serialize(data),
            correlation_id=correlation_id,
            reply_to=callback_queue.name,
            content_type="application/json",
        )
        await exchange.publish(message, routing_key=serialize_pattern(pattern))
        return await future

    async def emit(self, pattern: Any, data: Any) -> None:
        if self.event_handlers.get(serialize_pattern(pattern)) or self._exchange is None:
            await super().emit(pattern, data)
            return
        try:
            import aio_pika  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(self._install_hint) from exc
        await self._exchange.publish(
            aio_pika.Message(body=self.serializer.serialize(data), content_type="application/json"),
            routing_key=serialize_pattern(pattern),
        )

    async def listen_forever(self) -> None:
        if self._channel is None or self._exchange is None:
            while True:
                await asyncio.sleep(3600)
        channel = self._channel
        exchange = self._exchange
        assert channel is not None
        assert exchange is not None
        self._queue = await channel.declare_queue(self.queue_name or "", durable=bool(self.queue_name))
        queue = self._queue
        assert queue is not None
        await queue.bind(exchange, routing_key=self.routing_key)
        await queue.consume(self._handle_rmq_message)
        while True:
            await asyncio.sleep(3600)

    async def _handle_rmq_message(self, message: Any) -> None:
        async with message.process():
            pattern = getattr(message, "routing_key", "")
            data = self._load(getattr(message, "body", None))
            reply_to = getattr(message, "reply_to", None)
            correlation_id = getattr(message, "correlation_id", None)
            metadata = {"routing_key": pattern, "exchange": self.exchange_name}
            if reply_to:
                channel = self._channel
                assert channel is not None
                exchange = channel.default_exchange
                result = await self._dispatch_message(
                    pattern,
                    data,
                    raw=message,
                    metadata=metadata,
                    correlation_id=correlation_id,
                    reply_to=reply_to,
                )
                try:
                    import aio_pika  # type: ignore[reportMissingImports]
                except ImportError as exc:  # pragma: no cover
                    raise ImportError(self._install_hint) from exc
                await exchange.publish(
                    aio_pika.Message(
                        body=self.serializer.serialize(result),
                        correlation_id=correlation_id,
                    ),
                    routing_key=reply_to,
                )
                return
            try:
                await self._dispatch_event(pattern, data, raw=message, metadata=metadata)
            except Exception:
                logger.exception(
                    "Unhandled error in event handler(s) for pattern %r; listener continuing.",
                    pattern,
                )

    def create_context(self, **kwargs: Any) -> MicroserviceContext:
        metadata = kwargs.get("metadata") or {}
        kwargs.setdefault("transport", self.name)
        kwargs["headers"] = kwargs.get("headers") or {}
        kwargs["metadata"] = metadata
        return RmqContext(
            **kwargs,
            routing_key=metadata.get("routing_key"),
            exchange=metadata.get("exchange"),
        )


class KafkaTransport(_BrokerTransport):
    _broker = "kafka"
    context_type = KafkaContext
    _install_hint = "KafkaTransport requires aiokafka. Install it with: pip install aiokafka"

    def __init__(
        self,
        *,
        bootstrap_servers: str | list[str] | None = None,
        topic: str = "fanest.microservice",
        group_id: str = "fanest",
        reply_topic: str | None = None,
        producer: Any | None = None,
        consumer: Any | None = None,
        reply_consumer: Any | None = None,
        response_timeout: float = 5,
        adapter: TransportAdapter | None = None,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__(
            adapter=adapter,
            configured=(
                bootstrap_servers is not None
                or producer is not None
                or consumer is not None
                or reply_consumer is not None
            ),
            serializer=serializer,
            deserializer=deserializer,
        )
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.group_id = group_id
        self.reply_topic = reply_topic or f"{topic}.replies"
        self._producer = producer
        self._consumer = consumer
        self._reply_consumer = reply_consumer
        self.response_timeout = response_timeout
        self._pending_replies: dict[str, asyncio.Future[Any]] = {}
        self._reply_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        if self.adapter is not None:
            await self.adapter.connect()
        if (
            self._producer is None
            or self._consumer is None
            or self._reply_consumer is None
        ) and self.bootstrap_servers is not None:
            try:
                from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover
                raise ImportError(self._install_hint) from exc
            bootstrap_servers = cast(Any, self.bootstrap_servers)
            self._producer = self._producer or AIOKafkaProducer(bootstrap_servers=bootstrap_servers)
            self._consumer = self._consumer or AIOKafkaConsumer(
                self.topic,
                bootstrap_servers=bootstrap_servers,
                group_id=self.group_id,
            )
            self._reply_consumer = self._reply_consumer or AIOKafkaConsumer(
                self.reply_topic,
                bootstrap_servers=bootstrap_servers,
                group_id=f"{self.group_id}.reply.{uuid4()}",
            )
        for item in (self._producer, self._consumer, self._reply_consumer):
            start = getattr(item, "start", None)
            if start is not None:
                result = start()
                if inspect.isawaitable(result):
                    await result
        self.connected = True

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        if self._reply_task is not None:
            self._reply_task.cancel()
            try:
                await self._reply_task
            except asyncio.CancelledError:
                pass
            finally:
                self._reply_task = None
        for future in self._pending_replies.values():
            if not future.done():
                future.cancel()
        self._pending_replies.clear()
        for item in (self._reply_consumer, self._consumer, self._producer):
            stop = getattr(item, "stop", None)
            if stop is not None:
                result = stop()
                if inspect.isawaitable(result):
                    await result
        self.connected = False

    async def send(self, pattern: Any, data: Any) -> Any:
        if serialize_pattern(pattern) in self.message_handlers or self._producer is None:
            return await super().send(pattern, data)
        if self._reply_consumer is None:
            raise MicroserviceTransportError(
                "Kafka request/reply requires reply_consumer=... or bootstrap_servers=..."
            )
        self._ensure_reply_task()
        correlation_id = str(uuid4())
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending_replies[correlation_id] = future
        payload = {
            "__fanest_ms__": True,
            "pattern": serialize_pattern(pattern),
            "data": data,
            "headers": {},
            "correlation_id": correlation_id,
            "reply_to": self.reply_topic,
        }
        await self._producer.send_and_wait(
            self.topic,
            self.serializer.serialize(payload),
            key=serialize_pattern(pattern).encode(),
        )
        try:
            return await asyncio.wait_for(future, timeout=self.response_timeout)
        except asyncio.TimeoutError as exc:
            raise MicroserviceTimeoutError(
                f"No Kafka microservice response for pattern: {pattern!r}"
            ) from exc
        finally:
            self._pending_replies.pop(correlation_id, None)

    async def emit(self, pattern: Any, data: Any) -> None:
        if self.event_handlers.get(serialize_pattern(pattern)) or self._producer is None:
            await super().emit(pattern, data)
            return
        await self._producer.send_and_wait(
            self.topic,
            self.serializer.serialize(data),
            key=serialize_pattern(pattern).encode(),
        )

    async def listen_forever(self) -> None:
        if self._consumer is None:
            while True:
                await asyncio.sleep(3600)
        consumer = self._consumer
        assert consumer is not None
        async for message in consumer:
            key_pattern = _decode_text(getattr(message, "key", b"") or b"")
            payload = self._load(getattr(message, "value", None))
            pattern, data, headers, correlation_id, reply_to = self._unpack_kafka_payload(
                key_pattern,
                payload,
            )
            metadata = {
                "topic": getattr(message, "topic", None),
                "partition": getattr(message, "partition", None),
                "offset": getattr(message, "offset", None),
            }
            if reply_to and correlation_id:
                await self._handle_kafka_request(
                    pattern,
                    data,
                    message=message,
                    metadata=metadata,
                    headers=headers,
                    correlation_id=correlation_id,
                    reply_to=reply_to,
                )
                continue
            try:
                await self._dispatch_event(
                    pattern, data, raw=message, metadata=metadata, headers=headers
                )
            except Exception:
                logger.exception(
                    "Unhandled error in event handler(s) for pattern %r; listener continuing.",
                    pattern,
                )

    async def _handle_kafka_request(
        self,
        pattern: Any,
        data: Any,
        *,
        message: Any,
        metadata: dict[str, Any],
        headers: dict[str, Any],
        correlation_id: str,
        reply_to: str,
    ) -> None:
        if self._producer is None:
            raise MicroserviceTransportError("Kafka request handler requires a producer to publish replies")
        try:
            result = await self._dispatch_message(
                pattern,
                data,
                raw=message,
                metadata=metadata,
                headers=headers,
                correlation_id=correlation_id,
                reply_to=reply_to,
            )
            payload = {
                "__fanest_ms__": True,
                "correlation_id": correlation_id,
                "data": result,
                "error": "",
                "error_type": "",
            }
        except Exception as exc:
            payload = {
                "__fanest_ms__": True,
                "correlation_id": correlation_id,
                "data": None,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        await self._producer.send_and_wait(
            reply_to,
            self.serializer.serialize(payload),
            key=correlation_id.encode(),
        )

    def _ensure_reply_task(self) -> None:
        if self._reply_task is None or self._reply_task.done():
            self._reply_task = asyncio.create_task(self._consume_replies_forever())

    async def _consume_replies_forever(self) -> None:
        consumer = self._reply_consumer
        if consumer is None:
            return
        async for message in consumer:
            payload = self._load(getattr(message, "value", None))
            if not isinstance(payload, dict):
                continue
            correlation_id = str(payload.get("correlation_id") or _decode_text(getattr(message, "key", b"")))
            future = self._pending_replies.get(correlation_id)
            if future is None or future.done():
                continue
            if payload.get("error"):
                future.set_exception(
                    MicroserviceRemoteError(
                        str(payload["error"]),
                        error_type=str(payload.get("error_type") or "Error"),
                    )
                )
            else:
                future.set_result(payload.get("data"))

    def _unpack_kafka_payload(
        self,
        key_pattern: str,
        payload: Any,
    ) -> tuple[Any, Any, dict[str, Any], str | None, str | None]:
        if isinstance(payload, dict) and payload.get("__fanest_ms__"):
            return (
                payload.get("pattern") or key_pattern,
                payload.get("data"),
                dict(payload.get("headers") or {}),
                payload.get("correlation_id"),
                payload.get("reply_to"),
            )
        return key_pattern, payload, {}, None, None

    def create_context(self, **kwargs: Any) -> MicroserviceContext:
        metadata = kwargs.get("metadata") or {}
        kwargs.setdefault("transport", self.name)
        kwargs["headers"] = kwargs.get("headers") or {}
        kwargs["metadata"] = metadata
        return KafkaContext(
            **kwargs,
            topic=metadata.get("topic"),
            partition=metadata.get("partition"),
            offset=metadata.get("offset"),
        )


class GrpcTransport(_BrokerTransport):
    _broker = "grpc"
    context_type = GrpcContext
    _install_hint = "GrpcTransport requires grpcio. Install it with: pip install grpcio"

    def __init__(
        self,
        *,
        target: str | None = None,
        stub: Any | None = None,
        adapter: TransportAdapter | None = None,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__(
            adapter=adapter,
            configured=target is not None or stub is not None,
            serializer=serializer,
            deserializer=deserializer,
        )
        self.target = target
        self.stub = stub
        self._channel: Any | None = None

    async def connect(self) -> None:
        if self.adapter is not None:
            await self.adapter.connect()
        if self.stub is None and self.target is not None:
            try:
                import grpc  # type: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover
                raise ImportError(self._install_hint) from exc
            self._channel = grpc.aio.insecure_channel(self.target)
        self.connected = True

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        if self._channel is not None:
            close = getattr(self._channel, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
        self.connected = False

    async def send(self, pattern: Any, data: Any) -> Any:
        if serialize_pattern(pattern) in self.message_handlers or self.stub is None:
            return await super().send(pattern, data)
        method = getattr(self.stub, str(pattern), None)
        if method is None:
            raise MicroservicePatternError(f"gRPC stub does not expose method: {pattern}")
        result = method(data)
        if inspect.isawaitable(result):
            return await result
        return result

    async def emit(self, pattern: Any, data: Any) -> None:
        if self.event_handlers.get(serialize_pattern(pattern)) or self.stub is None:
            await super().emit(pattern, data)
            return
        await self.send(pattern, data)

    def create_context(self, **kwargs: Any) -> MicroserviceContext:
        kwargs.setdefault("transport", self.name)
        kwargs["headers"] = kwargs.get("headers") or {}
        kwargs["metadata"] = kwargs.get("metadata") or {}
        return GrpcContext(**kwargs, method=str(kwargs.get("pattern")))


class MqttTransport(_BrokerTransport):
    _broker = "mqtt"
    context_type = MqttContext
    _install_hint = "MqttTransport requires asyncio-mqtt. Install it with: pip install asyncio-mqtt"

    def __init__(
        self,
        *,
        client: Any | None = None,
        adapter: TransportAdapter | None = None,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__(
            adapter=adapter,
            configured=client is not None,
            serializer=serializer,
            deserializer=deserializer,
        )
        self._client = client

    async def connect(self) -> None:
        if self.adapter is not None:
            await self.adapter.connect()
        connect = getattr(self._client, "connect", None)
        if connect is not None:
            result = connect()
            if inspect.isawaitable(result):
                await result
        self.connected = True

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        disconnect = getattr(self._client, "disconnect", None)
        if disconnect is not None:
            result = disconnect()
            if inspect.isawaitable(result):
                await result
        self.connected = False

    async def send(self, pattern: Any, data: Any) -> Any:
        if serialize_pattern(pattern) in self.message_handlers or self.adapter is None:
            return await super().send(pattern, data)
        return await self.adapter.send(pattern, data)

    async def emit(self, pattern: Any, data: Any) -> None:
        if self.event_handlers.get(serialize_pattern(pattern)) or self.adapter is None:
            await super().emit(pattern, data)
            return
        await self.adapter.emit(pattern, data)

    def create_context(self, **kwargs: Any) -> MicroserviceContext:
        metadata = kwargs.get("metadata") or {}
        kwargs.setdefault("transport", self.name)
        kwargs["headers"] = kwargs.get("headers") or {}
        kwargs["metadata"] = metadata
        return MqttContext(**kwargs, topic=metadata.get("topic") or str(kwargs.get("pattern")))


class CustomTransport(_BrokerTransport):
    _broker = "custom"

    def __init__(
        self,
        adapter: TransportAdapter,
        *,
        serializer: MicroserviceSerializer | None = None,
        deserializer: MicroserviceSerializer | None = None,
    ) -> None:
        super().__init__(adapter=adapter, serializer=serializer, deserializer=deserializer)


@dataclass(frozen=True)
class GrpcProtoArtifacts:
    proto_file: Path
    output_dir: Path
    python_module: Path
    grpc_module: Path


class GrpcProtoLoader:
    @staticmethod
    def compile(
        proto_file: str | Path,
        *,
        output_dir: str | Path,
        include_dirs: list[str | Path] | None = None,
    ) -> GrpcProtoArtifacts:
        proto_path = Path(proto_file).resolve()
        if not proto_path.exists():
            raise FileNotFoundError(proto_path)
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        try:
            import grpc_tools.protoc  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "GrpcProtoLoader.compile requires grpcio-tools. "
                "Install it with: pip install grpcio-tools"
            ) from exc
        includes = [proto_path.parent, *(Path(item).resolve() for item in include_dirs or [])]
        args = [
            "grpc_tools.protoc",
            *(f"-I{include}" for include in includes),
            f"--python_out={output_path}",
            f"--grpc_python_out={output_path}",
            str(proto_path),
        ]
        exit_code = grpc_tools.protoc.main(args)
        if exit_code != 0:
            raise MicroserviceTransportError(
                f"grpc_tools.protoc failed for {proto_path} with exit code {exit_code}"
            )
        stem = proto_path.stem
        return GrpcProtoArtifacts(
            proto_file=proto_path,
            output_dir=output_path,
            python_module=output_path / f"{stem}_pb2.py",
            grpc_module=output_path / f"{stem}_pb2_grpc.py",
        )

    @staticmethod
    def compile_with_subprocess(
        proto_file: str | Path,
        *,
        output_dir: str | Path,
        include_dirs: list[str | Path] | None = None,
    ) -> GrpcProtoArtifacts:
        proto_path = Path(proto_file).resolve()
        if not proto_path.exists():
            raise FileNotFoundError(proto_path)
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        includes = [proto_path.parent, *(Path(item).resolve() for item in include_dirs or [])]
        command = [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            *(f"-I{include}" for include in includes),
            f"--python_out={output_path}",
            f"--grpc_python_out={output_path}",
            str(proto_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise MicroserviceTransportError(completed.stderr.strip() or completed.stdout.strip())
        stem = proto_path.stem
        return GrpcProtoArtifacts(
            proto_file=proto_path,
            output_dir=output_path,
            python_module=output_path / f"{stem}_pb2.py",
            grpc_module=output_path / f"{stem}_pb2_grpc.py",
        )

    @staticmethod
    def load_stub(
        grpc_module: Any,
        *,
        service: str,
        channel: Any,
    ) -> Any:
        stub_type = getattr(grpc_module, f"{service}Stub", None)
        if stub_type is None:
            raise MicroservicePatternError(f"gRPC module does not expose stub: {service}Stub")
        return stub_type(channel)


class MicroserviceServer:
    def __init__(self, root_module: type, *, transport: InMemoryTransport | None = None) -> None:
        self.root_module = root_module
        self.transport = transport or InMemoryTransport()
        self.scanner = ModuleScanner()
        self.container = FaNestContainer()
        self._compiled = False
        self._listen_task: Any | None = None

    def compile(self) -> "MicroserviceServer":
        if self._compiled:
            return self
        self.scanner.scan(self.root_module)
        if self.scanner.records:
            self.container.set_root_module(next(iter(self.scanner.records)))
        for module_key, record in self.scanner.records.items():
            self.container.register_module(
                module_key,
                providers=list(record.metadata.providers),
                imports=[self.scanner._module_key(imported) for imported in record.metadata.imports],
                exports=set(record.metadata.exports),
                global_module=record.metadata.global_module,
            )
        self._register_handlers()
        self._compiled = True
        return self

    def client(self) -> "ClientProxy":
        return ClientProxy(self.transport)

    async def listen(self) -> "MicroserviceServer":
        self.compile()
        await self.transport.connect()
        listen_forever = getattr(self.transport, "listen_forever", None)
        if listen_forever is not None and self._listen_task is None:
            import asyncio

            self._listen_task = asyncio.create_task(listen_forever())
        return self

    async def close(self) -> None:
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except BaseException as exc:
                import asyncio

                if not isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                self._listen_task = None
        await self.transport.close()

    @classmethod
    def create(
        cls,
        root_module: type,
        *,
        transport: str | Transport | InMemoryTransport = Transport.MEMORY,
        **transport_options: Any,
    ) -> "MicroserviceServer":
        if isinstance(transport, InMemoryTransport):
            return cls(root_module, transport=transport)
        transport_name = transport.value if isinstance(transport, Transport) else transport
        transports = {
            "memory": InMemoryTransport,
            "tcp": TcpTransport,
            "redis": RedisTransport,
            "nats": NatsTransport,
            "rabbitmq": RabbitMqTransport,
            "kafka": KafkaTransport,
            "grpc": GrpcTransport,
            "mqtt": MqttTransport,
            "custom": CustomTransport,
        }
        try:
            transport_class = transports[transport_name]
        except KeyError as exc:
            raise ValueError(f"Unknown microservice transport: {transport_name}") from exc
        return cls(root_module, transport=transport_class(**transport_options))

    def _register_handlers(self) -> None:
        for module_key, record in self.scanner.records.items():
            for provider in record.metadata.providers:
                provider_type = self._provider_type(provider)
                if provider_type is None:
                    continue
                for _, handler in inspect.getmembers(provider_type, predicate=inspect.isfunction):
                    self._register_handler(provider_type, handler, module_key)

    def _register_handler(self, provider: type, handler: Any, module_key: Any) -> None:
        message_pattern = getattr(handler, "__fanest_message_pattern__", None)
        if message_pattern is not None:
            self.transport.register_message(
                message_pattern,
                self._lazy_handler(provider, handler.__name__, module_key),
            )
        event_pattern = getattr(handler, "__fanest_event_pattern__", None)
        if event_pattern is not None:
            self.transport.register_event(
                event_pattern,
                self._lazy_handler(provider, handler.__name__, module_key),
            )

    def _provider_type(self, provider: Any) -> type | None:
        use_class = getattr(provider, "use_class", None)
        if use_class is not None:
            return use_class
        if inspect.isclass(provider):
            return provider
        return None

    def _lazy_handler(self, provider: type, method_name: str, module_key: Any):
        async def handler(data: Any, context: MicroserviceContext) -> Any:
            owns_scope = self.container.current_request_instances() is None
            request_scope = self.container.begin_request() if owns_scope else None
            try:
                instance = await self.container.resolve_async(provider, module_key=module_key)
                method = getattr(instance, method_name)
                execution_context = ExecutionContext(
                    handler=method,
                    controller=instance,
                    request=context,
                    kwargs={"data": data, "context": context},
                )
                try:
                    await self._run_guards(instance, method, execution_context, module_key)
                    execution_context.kwargs["data"] = await self._run_pipes(
                        instance,
                        method,
                        execution_context.kwargs["data"],
                        execution_context,
                        module_key,
                    )

                    async def call_handler() -> Any:
                        result = method(execution_context.kwargs["data"], context)
                        if inspect.isawaitable(result):
                            return await result
                        return result

                    return await self._run_interceptors(
                        instance,
                        method,
                        execution_context,
                        call_handler,
                        module_key,
                    )
                except Exception as exc:
                    handled = await self._run_filters(instance, method, execution_context, exc, module_key)
                    if handled is not _UNHANDLED:
                        return handled
                    raise
            finally:
                if owns_scope and request_scope is not None:
                    self.container.end_request(request_scope)

        setattr(handler, "__fanest_registration_key__", (module_key, provider, method_name, "microservice"))
        return handler

    async def _run_guards(
        self,
        provider: Any,
        handler: Callable[..., Any],
        context: ExecutionContext,
        module_key: Any,
    ) -> None:
        for guard in self._collect(provider, handler, "__fanest_guards__"):
            instance = await self._resolve_component_async(guard, module_key)
            result = instance.can_activate(context)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                raise MicroserviceTransportError("Forbidden")

    async def _run_pipes(
        self,
        provider: Any,
        handler: Callable[..., Any],
        data: Any,
        context: ExecutionContext,
        module_key: Any,
    ) -> Any:
        result = data
        for pipe in self._collect(provider, handler, "__fanest_pipes__"):
            instance = await self._resolve_component_async(pipe, module_key)
            transformed = instance.transform(
                result,
                {"name": "data", "handler": handler, "annotation": Any, "source": "message"},
            )
            if inspect.isawaitable(transformed):
                transformed = await transformed
            result = transformed
            context.kwargs["data"] = result
        return result

    async def _run_interceptors(
        self,
        provider: Any,
        handler: Callable[..., Any],
        context: ExecutionContext,
        call_handler: Callable[[], Any],
        module_key: Any,
    ) -> Any:
        interceptors = self._collect(provider, handler, "__fanest_interceptors__")

        async def dispatch(index: int) -> Any:
            if index >= len(interceptors):
                return await call_handler()
            instance = await self._resolve_component_async(interceptors[index], module_key)
            result = instance.intercept(context, lambda: dispatch(index + 1))
            if inspect.isawaitable(result):
                return await result
            return result

        return await dispatch(0)

    async def _run_filters(
        self,
        provider: Any,
        handler: Callable[..., Any],
        context: ExecutionContext,
        exc: Exception,
        module_key: Any,
    ) -> Any:
        for exception_filter in self._collect(provider, handler, "__fanest_filters__"):
            instance = await self._resolve_component_async(exception_filter, module_key)
            catch_types = getattr(instance.__class__, "__fanest_catch_exceptions__", (Exception,))
            if not isinstance(exc, catch_types):
                continue
            result = instance.catch(exc, context)
            if inspect.isawaitable(result):
                result = await result
            return result
        return _UNHANDLED

    def _collect(self, provider: Any, handler: Callable[..., Any], key: str) -> list[Any]:
        provider_values = getattr(provider.__class__, key, [])
        handler_values = self._metadata(handler, key, [])
        if key == "__fanest_filters__":
            return [*handler_values, *provider_values]
        return [*provider_values, *handler_values]

    async def _resolve_component_async(self, component: Any, module_key: Any) -> Any:
        if inspect.isclass(component):
            return await self.container.resolve_async(component, module_key=module_key)
        return component

    def _metadata(self, target: Any, key: str, default: Any = None) -> Any:
        if hasattr(target, key):
            return getattr(target, key)
        func = getattr(target, "__func__", None)
        if func is not None and hasattr(func, key):
            return getattr(func, key)
        return default


class ClientProxy:
    def __init__(
        self,
        transport: InMemoryTransport,
        *,
        timeout: float | None = None,
        retries: int = 0,
        retry_delay: float = 0,
    ):
        self.transport = transport
        self.options = ClientProxyOptions(timeout=timeout, retries=retries, retry_delay=retry_delay)
        self.connected = False

    async def connect(self) -> "ClientProxy":
        connect = getattr(self.transport, "connect", None)
        if connect is not None:
            result = connect()
            if inspect.isawaitable(result):
                await result
        self.connected = True
        return self

    async def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        self.connected = False

    async def send(self, pattern: Any, data: Any) -> Any:
        if not self.connected:
            await self.connect()
        attempt = 0
        while True:
            try:
                call = self.transport.send(pattern, data)
                if self.options.timeout is None:
                    return await call
                return await asyncio.wait_for(call, timeout=self.options.timeout)
            except asyncio.TimeoutError as exc:
                if self.options.timeout is None:
                    error: BaseException = exc
                else:
                    error = MicroserviceTimeoutError(
                        f"Microservice request timed out for pattern: {pattern!r}"
                    )
                    error.__cause__ = exc
            except Exception as exc:
                error = exc
            if attempt >= self.options.retries:
                raise error
            attempt += 1
            if self.options.retry_delay > 0:
                await asyncio.sleep(self.options.retry_delay)

    async def emit(self, pattern: Any, data: Any) -> None:
        if not self.connected:
            await self.connect()
        await self.transport.emit(pattern, data)


class ClientProxyFactory:
    @staticmethod
    def create(
        options: dict[str, Any] | None = None,
        /,
        *,
        transport: str | Transport | InMemoryTransport = Transport.MEMORY,
        timeout: float | None = None,
        retries: int = 0,
        retry_delay: float = 0,
        **transport_options: Any,
    ) -> ClientProxy:
        if options is not None:
            transport = options.get("transport", transport)
            nested_options = options.get("options", {})
            timeout = options.get("timeout", timeout)
            retries = options.get("retries", retries)
            retry_delay = options.get("retry_delay", retry_delay)
            transport_options = {
                **nested_options,
                **{
                    key: value
                    for key, value in options.items()
                    if key not in {"transport", "options", "timeout", "retries", "retry_delay"}
                },
            }
        server = MicroserviceServer.create(
            _EmptyMicroserviceModule,
            transport=transport,
            **transport_options,
        ).compile()
        return ClientProxy(server.transport, timeout=timeout, retries=retries, retry_delay=retry_delay)


def client_token(name: str):
    return f"FANEST_CLIENT:{name}"


def InjectClient(name: str = "default"):
    return Inject(client_token(name))


class ClientsModule:
    @staticmethod
    def register(*clients: dict[str, Any], is_global: bool = False) -> type:
        providers: list[ValueProvider] = []
        for client in clients:
            name = client.get("name", "default")
            transport = client.get("transport", Transport.MEMORY)
            options = client.get("options", {})
            if "redis_url" in client:
                options = {**options, "url": client["redis_url"]}
            if "url" in client:
                options = {**options, "url": client["url"]}
            providers.append(
                use_value(
                    client_token(name),
                    ClientProxyFactory.create(
                        transport=transport,
                        timeout=client.get("timeout"),
                        retries=client.get("retries", 0),
                        retry_delay=client.get("retry_delay", 0),
                        **options,
                    ),
                )
            )

        @Module(providers=providers, exports=[client.provide for client in providers], global_module=is_global)
        class DynamicClientsModule:
            pass

        return DynamicClientsModule


@Module()
class _EmptyMicroserviceModule:
    pass
