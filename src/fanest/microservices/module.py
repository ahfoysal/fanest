import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fanest.core.container import FaNestContainer
from fanest.core.metadata import ValueProvider
from fanest.core.scanner import ModuleScanner
from fanest import Inject, Module, use_value


@dataclass(frozen=True)
class MicroserviceContext:
    pattern: str
    data: Any
    transport: str = "memory"


class Transport(str, Enum):
    MEMORY = "memory"
    REDIS = "redis"
    NATS = "nats"
    RABBITMQ = "rabbitmq"
    KAFKA = "kafka"
    GRPC = "grpc"


class MicroservicePatternError(KeyError):
    pass


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
        handlers = self.event_handlers.setdefault(pattern, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            handlers.append(handler)

    async def send(self, pattern: str, data: Any) -> Any:
        if pattern not in self.message_handlers:
            raise MicroservicePatternError(f"No message handler registered for pattern: {pattern}")
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

    def _handler_key(self, handler: Any) -> Any:
        return getattr(handler, "__fanest_registration_key__", handler)


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
        return self

    def client(self) -> "ClientProxy":
        return ClientProxy(self.transport)

    @classmethod
    def create(cls, root_module: type, *, transport: str | Transport = Transport.MEMORY) -> "MicroserviceServer":
        transport_name = transport.value if isinstance(transport, Transport) else transport
        transports = {
            "memory": InMemoryTransport,
            "redis": RedisTransport,
            "nats": NatsTransport,
            "rabbitmq": RabbitMqTransport,
            "kafka": KafkaTransport,
            "grpc": GrpcTransport,
        }
        try:
            transport_class = transports[transport_name]
        except KeyError as exc:
            raise ValueError(f"Unknown microservice transport: {transport_name}") from exc
        return cls(root_module, transport=transport_class())

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
                result = getattr(instance, method_name)(data, context)
                if inspect.isawaitable(result):
                    return await result
                return result
            finally:
                if owns_scope and request_scope is not None:
                    self.container.end_request(request_scope)

        setattr(handler, "__fanest_registration_key__", (module_key, provider, method_name, "microservice"))
        return handler


class ClientProxy:
    def __init__(self, transport: InMemoryTransport):
        self.transport = transport
        self.connected = False

    async def connect(self) -> "ClientProxy":
        self.connected = True
        return self

    async def close(self) -> None:
        self.connected = False

    async def send(self, pattern: str, data: Any) -> Any:
        if not self.connected:
            await self.connect()
        return await self.transport.send(pattern, data)

    async def emit(self, pattern: str, data: Any) -> None:
        if not self.connected:
            await self.connect()
        await self.transport.emit(pattern, data)


class ClientProxyFactory:
    @staticmethod
    def create(*, transport: str | Transport = Transport.MEMORY) -> ClientProxy:
        server = MicroserviceServer.create(_EmptyMicroserviceModule, transport=transport).compile()
        return server.client()


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
            providers.append(use_value(client_token(name), ClientProxyFactory.create(transport=transport)))

        @Module(providers=providers, exports=[client.provide for client in providers], global_module=is_global)
        class DynamicClientsModule:
            pass

        return DynamicClientsModule


@Module()
class _EmptyMicroserviceModule:
    pass
