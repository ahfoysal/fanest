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


def Injectable(scope: str = "singleton") -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, "__fanest_provider__", ProviderMetadata(scope=scope))
        return cls

    return decorator


def Controller(prefix: str = "") -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, "__fanest_controller__", ControllerMetadata(prefix=prefix))
        setattr(cls, "__fanest_provider__", ProviderMetadata(scope="request"))
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


def _set_route_option(handler: Callable[..., Any], key: str, value: Any) -> None:
    route_metadata: RouteMetadata | None = getattr(handler, "__fanest_route__", None)
    if route_metadata is not None:
        route_metadata.options[key] = value
        return
    pending = dict(getattr(handler, "__fanest_pending_route_options__", {}))
    pending[key] = value
    setattr(handler, "__fanest_pending_route_options__", pending)


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


def Options(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route("OPTIONS", path, **options)


def Head(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route("HEAD", path, **options)


def All(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route("ALL", path, **options)


def HttpCode(status_code: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        _set_route_option(handler, "status_code", status_code)
        return handler

    return decorator


def Redirect(url: str, status_code: int = 302) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_redirect__", {"url": url, "status_code": status_code})
        return handler

    return decorator


def SetMetadata(key: str, value: Any) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        metadata = dict(getattr(target, "__fanest_metadata__", {}))
        metadata[key] = value
        setattr(target, "__fanest_metadata__", metadata)
        return target

    return decorator


def Version(version: str | int) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        setattr(target, "__fanest_version__", str(version))
        return target

    return decorator


def ResponseModel(model: Any, **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        _set_route_option(handler, "response_model", model)
        for key, value in options.items():
            _set_route_option(handler, key, value)
        return handler

    return decorator


def SetHeader(name: str, value: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        _append_metadata(handler, "__fanest_response_headers__", ((name, value),))
        return handler

    return decorator


def Sse(path: str = "", **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_sse__", True)
        return route("GET", path, **options)(handler)

    return decorator


def Render(template: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_render_template__", template)
        return handler

    return decorator


def Body(name: str | None = None, default: Any = ...) -> ParameterSource:
    return ParameterSource(source="body", name=name, default=default)


def Param(name: str | None = None, default: Any = ...) -> ParameterSource:
    return ParameterSource(source="path", name=name, default=default)


def Query(name: str | None = None, default: Any = ...) -> ParameterSource:
    return ParameterSource(source="query", name=name, default=default)


def Header(name: str | None = None, default: Any = None) -> ParameterSource:
    return ParameterSource(source="header", name=name, default=default)


def Cookie(name: str | None = None, default: Any = None) -> ParameterSource:
    return ParameterSource(source="cookie", name=name, default=default)


def Form(name: str | None = None, default: Any = ...) -> ParameterSource:
    return ParameterSource(source="form", name=name, default=default)


def UploadedFile(name: str = "file") -> ParameterSource:
    return ParameterSource(source="file", name=name)


def UploadedFiles(name: str = "files") -> ParameterSource:
    return ParameterSource(source="files", name=name, default=[])


def Req() -> ParameterSource:
    return ParameterSource(source="request")


def Res() -> ParameterSource:
    return ParameterSource(source="response")


def Ip() -> ParameterSource:
    return ParameterSource(source="ip")


def Session(default: Any = None) -> ParameterSource:
    return ParameterSource(source="session", default=default)


def BackgroundTasks() -> ParameterSource:
    return ParameterSource(source="background_tasks")


def create_param_decorator(factory: Callable[[Any, Any], Any]):
    def decorator(data: Any = None) -> ParameterSource:
        return ParameterSource(source="custom", name=None, default={"factory": factory, "data": data})

    return decorator


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
