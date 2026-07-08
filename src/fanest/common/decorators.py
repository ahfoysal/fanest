from collections.abc import Callable
from typing import Any, TypeVar

from fanest.core.metadata import (
    ControllerMetadata,
    GatewayMetadata,
    MessageMetadata,
    ParameterSource,
    ProviderMetadata,
    RouteMetadata,
)

T = TypeVar("T")


def Injectable() -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, "__fanest_provider__", ProviderMetadata())
        return cls

    return decorator


def Controller(prefix: str = "") -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, "__fanest_controller__", ControllerMetadata(prefix=prefix))
        return cls

    return decorator


def WebSocketGateway(path: str = "/ws") -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, "__fanest_gateway__", GatewayMetadata(path=path))
        setattr(cls, "__fanest_provider__", ProviderMetadata())
        return cls

    return decorator


def SubscribeMessage(event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_message__", MessageMetadata(event=event))
        return handler

    return decorator


def _append_metadata(target: Any, key: str, values: tuple[Any, ...]) -> None:
    existing = list(getattr(target, key, []))
    existing.extend(values)
    setattr(target, key, existing)


def route(
    method: str, path: str = "", **options: Any
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_route__", RouteMetadata(method=method, path=path, options=options))
        return handler

    return decorator


def Get(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route("GET", path, **options)


def Post(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route("POST", path, **options)


def Put(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route("PUT", path, **options)


def Patch(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route("PATCH", path, **options)


def Delete(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route("DELETE", path, **options)


def Body(name: str | None = None, default: Any = ...) -> ParameterSource:
    return ParameterSource(source="body", name=name, default=default)


def Param(name: str | None = None, default: Any = ...) -> ParameterSource:
    return ParameterSource(source="path", name=name, default=default)


def Query(name: str | None = None, default: Any = ...) -> ParameterSource:
    return ParameterSource(source="query", name=name, default=default)


def Header(name: str | None = None, default: Any = None) -> ParameterSource:
    return ParameterSource(source="header", name=name, default=default)


def Req() -> ParameterSource:
    return ParameterSource(source="request")


def UseGuards(*guards: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _append_metadata(target, "__fanest_guards__", guards)
        return target

    return decorator


def UsePipes(*pipes: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _append_metadata(target, "__fanest_pipes__", pipes)
        return target

    return decorator


def UseInterceptors(*interceptors: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _append_metadata(target, "__fanest_interceptors__", interceptors)
        return target

    return decorator


def UseFilters(*filters: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        _append_metadata(target, "__fanest_filters__", filters)
        return target

    return decorator
