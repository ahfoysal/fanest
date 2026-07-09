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


def Controller(prefix: str = "", *, host: str | None = None) -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, "__fanest_controller__", ControllerMetadata(prefix=prefix, host=host))
        setattr(cls, "__fanest_provider__", ProviderMetadata(scope="request"))
        return cls

    return decorator


def WebSocketGateway(
    path: str = "/ws",
    *,
    namespace: str | None = None,
    transport: str = "websocket",
    transports: list[str] | tuple[str, ...] | None = None,
    **options: Any,
) -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        gateway_path = namespace or path
        gateway_options = dict(options)
        if transports is not None:
            gateway_options["transports"] = tuple(transports)
        setattr(
            cls,
            "__fanest_gateway__",
            GatewayMetadata(
                path=gateway_path,
                namespace=namespace,
                transport=transport,
                options=gateway_options,
            ),
        )
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


def Version(version: str | int | list[str | int] | tuple[str | int, ...]) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        if isinstance(version, list | tuple):
            setattr(target, "__fanest_version__", tuple(str(item) for item in version))
        else:
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


def Body(name: str | None = None, *pipes: Any, default: Any = ...) -> Any:
    return ParameterSource(source="body", name=name, default=default, pipes=pipes)


def Param(name: str | None = None, *pipes: Any, default: Any = ...) -> Any:
    return ParameterSource(source="path", name=name, default=default, pipes=pipes)


def Query(name: str | None = None, *pipes: Any, default: Any = ...) -> Any:
    return ParameterSource(source="query", name=name, default=default, pipes=pipes)


def Header(name: str | None = None, *pipes: Any, default: Any = None) -> Any:
    return ParameterSource(source="header", name=name, default=default, pipes=pipes)


def Headers(name: str | None = None, *pipes: Any, default: Any = None) -> Any:
    return Header(name, *pipes, default=default)


def Cookie(name: str | None = None, *pipes: Any, default: Any = None) -> Any:
    return ParameterSource(source="cookie", name=name, default=default, pipes=pipes)


def Form(name: str | None = None, *pipes: Any, default: Any = ...) -> Any:
    return ParameterSource(source="form", name=name, default=default, pipes=pipes)


def UploadedFile(name: str = "file", *, default: Any = ...) -> Any:
    return ParameterSource(source="file", name=name, default=default)


def UploadedFiles(name: str = "files", *, default: Any = ...) -> Any:
    return ParameterSource(source="files", name=name, default=default)


def Req() -> Any:
    return ParameterSource(source="request")


def Res(*, passthrough: bool = False) -> Any:
    return ParameterSource(source="response", default={"passthrough": passthrough})


def Ip() -> Any:
    return ParameterSource(source="ip")


def HostParam(name: str | None = None, default: Any = None) -> Any:
    return ParameterSource(source="host", name=name, default=default)


def Session(default: Any = None) -> Any:
    return ParameterSource(source="session", default=default)


def State(name: str | None = None, default: Any = None) -> Any:
    return ParameterSource(source="state", name=name, default=default)


def BackgroundTasks() -> Any:
    return ParameterSource(source="background_tasks")


def MessageBody(name: str | None = None, *pipes: Any, default: Any = None) -> Any:
    return ParameterSource(source="message_body", name=name, default=default, pipes=pipes)


def ConnectedSocket() -> Any:
    return ParameterSource(source="connected_socket")


def Ack() -> Any:
    return ParameterSource(source="ack")


def create_param_decorator(factory: Callable[[Any, Any], Any]):
    def decorator(data: Any = None) -> Any:
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
