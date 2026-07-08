import inspect
from dataclasses import dataclass
from typing import Any

from fanest.core.container import FaNestContainer
from fanest.core.scanner import ModuleScanner


@dataclass(frozen=True)
class MicroserviceContext:
    pattern: str
    data: Any
    transport: str = "memory"


def MessagePattern(pattern: str):
    def decorator(handler):
        setattr(handler, "__fanest_message_pattern__", pattern)
        return handler

    return decorator


def EventPattern(pattern: str):
    def decorator(handler):
        setattr(handler, "__fanest_event_pattern__", pattern)
        return handler

    return decorator


class InMemoryTransport:
    def __init__(self, name: str = "memory") -> None:
        self.name = name
        self.message_handlers: dict[str, Any] = {}
        self.event_handlers: dict[str, list[Any]] = {}

    def register_message(self, pattern: str, handler: Any) -> None:
        self.message_handlers[pattern] = handler

    def register_event(self, pattern: str, handler: Any) -> None:
        self.event_handlers.setdefault(pattern, []).append(handler)

    async def send(self, pattern: str, data: Any) -> Any:
        handler = self.message_handlers[pattern]
        result = handler(data, MicroserviceContext(pattern=pattern, data=data, transport=self.name))
        if inspect.isawaitable(result):
            return await result
        return result

    async def emit(self, pattern: str, data: Any) -> None:
        for handler in self.event_handlers.get(pattern, []):
            result = handler(data, MicroserviceContext(pattern=pattern, data=data, transport=self.name))
            if inspect.isawaitable(result):
                await result


class RedisTransport(InMemoryTransport):
    def __init__(self) -> None:
        super().__init__("redis")


class NatsTransport(InMemoryTransport):
    def __init__(self) -> None:
        super().__init__("nats")


class RabbitMqTransport(InMemoryTransport):
    def __init__(self) -> None:
        super().__init__("rabbitmq")


class KafkaTransport(InMemoryTransport):
    def __init__(self) -> None:
        super().__init__("kafka")


class GrpcTransport(InMemoryTransport):
    def __init__(self) -> None:
        super().__init__("grpc")


class MicroserviceServer:
    def __init__(self, root_module: type, *, transport: InMemoryTransport | None = None) -> None:
        self.root_module = root_module
        self.transport = transport or InMemoryTransport()
        self.scanner = ModuleScanner()
        self.container = FaNestContainer()

    def compile(self) -> "MicroserviceServer":
        self.scanner.scan(self.root_module)
        for provider in self.scanner.providers:
            self.container.register(provider)
        self._register_handlers()
        return self

    def client(self) -> "ClientProxy":
        return ClientProxy(self.transport)

    @classmethod
    def create(cls, root_module: type, *, transport: str = "memory") -> "MicroserviceServer":
        transports = {
            "memory": InMemoryTransport,
            "redis": RedisTransport,
            "nats": NatsTransport,
            "rabbitmq": RabbitMqTransport,
            "kafka": KafkaTransport,
            "grpc": GrpcTransport,
        }
        try:
            transport_class = transports[transport]
        except KeyError as exc:
            raise ValueError(f"Unknown microservice transport: {transport}") from exc
        return cls(root_module, transport=transport_class())

    def _register_handlers(self) -> None:
        for provider in self.scanner.providers:
            instance = self.container.resolve(self.container.provider_token(provider))
            for _, handler in inspect.getmembers(instance, predicate=callable):
                message_pattern = getattr(handler, "__fanest_message_pattern__", None)
                if message_pattern is not None:
                    self.transport.register_message(message_pattern, handler)
                event_pattern = getattr(handler, "__fanest_event_pattern__", None)
                if event_pattern is not None:
                    self.transport.register_event(event_pattern, handler)


class ClientProxy:
    def __init__(self, transport: InMemoryTransport):
        self.transport = transport

    async def send(self, pattern: str, data: Any) -> Any:
        return await self.transport.send(pattern, data)

    async def emit(self, pattern: str, data: Any) -> None:
        await self.transport.emit(pattern, data)
