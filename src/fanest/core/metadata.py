from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RouteMetadata:
    method: str
    path: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControllerMetadata:
    prefix: str = ""


@dataclass(frozen=True)
class GatewayMetadata:
    path: str = "/ws"


@dataclass(frozen=True)
class MessageMetadata:
    event: str


@dataclass(frozen=True)
class ProviderMetadata:
    scope: str = "singleton"


@dataclass(frozen=True)
class InjectionToken:
    name: str


@dataclass(frozen=True)
class InjectMarker:
    token: Any
    optional: bool = False
    default: Any = None


@dataclass(frozen=True)
class ForwardRef:
    factory: Any


@dataclass(frozen=True)
class ClassProvider:
    provide: Any
    use_class: type


@dataclass(frozen=True)
class ValueProvider:
    provide: Any
    use_value: Any


@dataclass(frozen=True)
class FactoryProvider:
    provide: Any
    use_factory: Any
    inject: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class ExistingProvider:
    provide: Any
    use_existing: Any


ProviderDefinition = type | ClassProvider | ValueProvider | FactoryProvider | ExistingProvider | ForwardRef


@dataclass(frozen=True)
class ModuleMetadata:
    imports: list[type] = field(default_factory=list)
    controllers: list[type] = field(default_factory=list)
    providers: list[ProviderDefinition] = field(default_factory=list)
    gateways: list[type] = field(default_factory=list)
    middlewares: list[Any] = field(default_factory=list)
    exports: list[type] = field(default_factory=list)
    global_module: bool = False


@dataclass(frozen=True)
class ParameterSource:
    source: str
    name: str | None = None
    default: Any = ...


@dataclass(frozen=True)
class ExecutionContext:
    handler: Any
    controller: Any
    request: Any
    kwargs: dict[str, Any]
