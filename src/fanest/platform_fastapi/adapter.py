import inspect
import json
from collections.abc import Callable
from typing import Any

from fastapi import Body as FastBody
from fastapi import BackgroundTasks as FastBackgroundTasks
from fastapi import Cookie, FastAPI, File, Form as FastForm, Header, HTTPException, Path, Query, Request, Response, WebSocket
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.websockets import WebSocketDisconnect

from fanest.common.responses import StreamableFile
from fanest.core.container import FaNestContainer
from fanest.core.metadata import (
    ControllerMetadata,
    ExecutionContext,
    GatewayMetadata,
    MessageMetadata,
    ParameterSource,
    RouteMetadata,
)
from fanest.websockets import WebSocketManager


class FastApiAdapter:
    def __init__(
        self,
        *,
        app: FastAPI,
        container: FaNestContainer,
        global_prefix: str = "",
        global_guards: list[object] | None = None,
        global_pipes: list[object] | None = None,
        global_interceptors: list[object] | None = None,
        global_filters: list[object] | None = None,
    ) -> None:
        self.app = app
        self.container = container
        self.global_prefix = global_prefix
        self.global_guards = global_guards or []
        self.global_pipes = global_pipes or []
        self.global_interceptors = global_interceptors or []
        self.global_filters = global_filters or []

    def register_controllers(self, controllers: list[type]) -> None:
        for controller in controllers:
            self._register_controller(controller)

    def register_gateways(self, gateways: list[type]) -> None:
        for gateway in gateways:
            self._register_gateway(gateway)

    def _register_controller(self, controller: type) -> None:
        controller_metadata: ControllerMetadata | None = getattr(
            controller, "__fanest_controller__", None
        )
        if controller_metadata is None:
            raise TypeError(f"{controller.__name__} is not a FaNest controller.")

        routes: list[tuple[str, Callable[..., Any], RouteMetadata]] = []
        for _, handler in inspect.getmembers(controller, predicate=inspect.isfunction):
            route_metadata: RouteMetadata | None = getattr(handler, "__fanest_route__", None)
            if route_metadata is None:
                continue
            routes.append((route_metadata.path, handler, route_metadata))

        for _, handler, route_metadata in sorted(routes, key=self._route_sort_key):
            version = self._metadata(handler, "__fanest_version__") or getattr(
                controller, "__fanest_version__", None
            )
            path = self._join_paths(
                self.global_prefix,
                f"v{version}" if version else "",
                controller_metadata.prefix,
                route_metadata.path,
            )
            endpoint = self._endpoint(controller, handler.__name__, handler)
            route_options = dict(route_metadata.options)
            tags = getattr(controller, "__fanest_swagger_tags__", None)
            if tags and "tags" not in route_options:
                route_options["tags"] = tags
            pending_responses = getattr(handler, "__fanest_pending_responses__", None)
            if pending_responses:
                route_options["responses"] = pending_responses
            pending_route_options = getattr(handler, "__fanest_pending_route_options__", None)
            if pending_route_options:
                route_options.update(pending_route_options)
            pending_openapi_extra = getattr(handler, "__fanest_pending_openapi_extra__", None)
            if pending_openapi_extra:
                route_options["openapi_extra"] = pending_openapi_extra
            if self._metadata(handler, "__fanest_bearer_auth__") or getattr(
                controller, "__fanest_bearer_auth__", False
            ):
                extra = dict(route_options.get("openapi_extra", {}))
                extra["security"] = [*extra.get("security", []), {"bearer": []}]
                route_options["openapi_extra"] = extra
            securities = [
                *getattr(controller, "__fanest_security__", []),
                *self._metadata(handler, "__fanest_security__", []),
            ]
            if securities:
                extra = dict(route_options.get("openapi_extra", {}))
                extra["security"] = [*extra.get("security", []), *securities]
                route_options["openapi_extra"] = extra
            self.app.add_api_route(
                path,
                endpoint,
                methods=self._route_methods(route_metadata.method),
                **route_options,
            )

    def _route_sort_key(self, route: tuple[str, Callable[..., Any], RouteMetadata]) -> tuple[int, int]:
        path = route[0]
        dynamic_segments = path.count("{")
        return (dynamic_segments, -len(path))

    def _register_gateway(self, gateway: type) -> None:
        gateway_metadata: GatewayMetadata | None = getattr(gateway, "__fanest_gateway__", None)
        if gateway_metadata is None:
            raise TypeError(f"{gateway.__name__} is not a FaNest gateway.")

        instance = self.container.resolve(gateway)
        handlers: dict[str, Callable[..., Any]] = {}
        for _, handler in inspect.getmembers(instance, predicate=inspect.ismethod):
            message_metadata: MessageMetadata | None = getattr(handler, "__fanest_message__", None)
            if message_metadata is not None:
                handlers[message_metadata.event] = handler

        path = self._join_paths(self.global_prefix, gateway_metadata.path)

        async def websocket_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            connect_hook = getattr(instance, "on_connect", None)
            if connect_hook is not None:
                result = connect_hook(websocket)
                if inspect.isawaitable(result):
                    await result
            try:
                while True:
                    payload = await websocket.receive_json()
                    event = payload.get("event")
                    data = payload.get("data")
                    handler = handlers.get(event)
                    if handler is None:
                        await websocket.send_json({"event": "error", "data": "Unknown event"})
                        continue
                    context = ExecutionContext(
                        handler=handler,
                        controller=instance,
                        request=websocket,
                        kwargs={"data": data, "websocket": websocket},
                    )
                    try:
                        await self._run_guards(instance, handler, context)
                        data = await self._run_websocket_pipes(instance, handler, data, context)
                    except Exception as exc:
                        await websocket.send_json({"event": "error", "data": str(exc)})
                        continue
                    result = handler(data, websocket)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is not None:
                        await websocket.send_json({"event": event, "data": result})
            except WebSocketDisconnect:
                pass
            finally:
                self.container.resolve(WebSocketManager).disconnect(websocket)
                disconnect_hook = getattr(instance, "on_disconnect", None)
                if disconnect_hook is not None:
                    result = disconnect_hook(websocket)
                    if inspect.isawaitable(result):
                        await result

        self.app.add_api_websocket_route(path, websocket_endpoint)

    def _endpoint(
        self, controller_class: type, handler_name: str, handler_function: Callable[..., Any]
    ) -> Callable[..., Any]:
        async def endpoint(
            request: Request,
            response: Response,
            background_tasks: FastBackgroundTasks,
            **kwargs: Any,
        ) -> Any:
            request_scope = self.container.begin_request()
            controller = self.container.resolve(controller_class)
            handler = getattr(controller, handler_name)
            context = ExecutionContext(
                handler=handler,
                controller=controller,
                request=request,
                kwargs=kwargs,
            )
            try:
                await self._run_guards(controller, handler, context)
                try:
                    context.kwargs.update(self._bind_request_parameters(handler, request, kwargs))
                    context.kwargs.update(self._bind_response_parameters(handler, response, kwargs))
                    context.kwargs.update(
                        self._bind_background_tasks_parameters(
                            handler, background_tasks, context.kwargs
                        )
                    )
                    context.kwargs.update(self._bind_ip_parameters(handler, request, context.kwargs))
                    context.kwargs.update(
                        self._bind_session_parameters(handler, request, context.kwargs)
                    )
                    context.kwargs.update(self._bind_state_parameters(handler, request, kwargs))
                    context.kwargs.update(self._bind_custom_parameters(handler, context, kwargs))
                    kwargs = await self._run_pipes(controller, handler, context)

                    async def call_handler() -> Any:
                        result = handler(**kwargs)
                        if inspect.isawaitable(result):
                            result = await result
                        response_headers = dict(self._response_headers(controller, handler))
                        for name, value in response_headers.items():
                            response.headers[name] = value
                        if self._metadata(handler, "__fanest_sse__", False):
                            return self._sse_response(result, response_headers)
                        if isinstance(result, StreamableFile):
                            return result.to_response(response_headers)
                        redirect = self._metadata(handler, "__fanest_redirect__")
                        if redirect is not None:
                            if isinstance(result, dict) and result.get("url"):
                                return RedirectResponse(
                                    result["url"],
                                    status_code=result.get("status_code", redirect["status_code"]),
                                )
                            return RedirectResponse(redirect["url"], status_code=redirect["status_code"])
                        return result

                    return await self._run_interceptors(controller, handler, context, call_handler)
                except Exception as exc:
                    handled = await self._run_filters(controller, handler, context, exc)
                    if handled is not None:
                        return handled
                    raise
            finally:
                self.container.end_request(request_scope)

        endpoint.__name__ = handler_name
        endpoint.__signature__ = self._build_signature(handler_function)  # type: ignore[attr-defined]
        return endpoint

    def _build_signature(self, handler: Callable[..., Any]) -> inspect.Signature:
        original = inspect.signature(handler)
        parameters = [
            inspect.Parameter(
                "request",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=Request,
            ),
            inspect.Parameter(
                "response",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=Response,
            ),
            inspect.Parameter(
                "background_tasks",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=FastBackgroundTasks,
            ),
        ]
        for name, parameter in original.parameters.items():
            if name == "self":
                continue
            source = parameter.default
            annotation = parameter.annotation
            if isinstance(source, ParameterSource):
                if source.source in {
                    "request",
                    "response",
                    "custom",
                    "ip",
                    "session",
                    "background_tasks",
                }:
                    continue
                default = self._fastapi_default(source, name)
            else:
                default = parameter.default
            parameters.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=annotation,
                )
            )
        return inspect.Signature(parameters=parameters, return_annotation=original.return_annotation)

    def _fastapi_default(self, source: ParameterSource, fallback_name: str) -> Any:
        alias = source.name or fallback_name
        if source.source == "body":
            return FastBody(source.default, alias=source.name)
        if source.source == "path":
            return Path(source.default, alias=alias)
        if source.source == "query":
            return Query(source.default, alias=source.name)
        if source.source == "header":
            return Header(source.default, alias=source.name)
        if source.source == "cookie":
            return Cookie(source.default, alias=source.name)
        if source.source == "file":
            return File(..., alias=source.name)
        if source.source == "files":
            return File(source.default, alias=source.name)
        if source.source == "form":
            return FastForm(source.default, alias=source.name)
        if source.source == "request":
            return inspect.Parameter.empty
        if source.source == "state":
            return None
        return source.default

    async def _run_guards(
        self, controller: Any, handler: Callable[..., Any], context: ExecutionContext
    ) -> None:
        for guard in self._collect(controller, handler, "__fanest_guards__"):
            instance = self._resolve_component(guard)
            result = instance.can_activate(context)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                raise HTTPException(status_code=403, detail="Forbidden")

    async def _run_pipes(
        self, controller: Any, handler: Callable[..., Any], context: ExecutionContext
    ) -> dict[str, Any]:
        kwargs = dict(context.kwargs)
        for pipe in self._collect(controller, handler, "__fanest_pipes__"):
            instance = self._resolve_component(pipe)
            for name, value in list(kwargs.items()):
                parameter = inspect.signature(handler).parameters.get(name)
                annotation = parameter.annotation if parameter is not None else None
                result = instance.transform(
                    value,
                    {"name": name, "handler": handler, "annotation": annotation},
                )
                if inspect.isawaitable(result):
                    result = await result
                kwargs[name] = result
        context.kwargs.update(kwargs)
        return kwargs

    async def _run_websocket_pipes(
        self,
        gateway: Any,
        handler: Callable[..., Any],
        data: Any,
        context: ExecutionContext,
    ) -> Any:
        result = data
        parameter = inspect.signature(handler).parameters.get("data")
        annotation = parameter.annotation if parameter is not None else None
        for pipe in self._collect(gateway, handler, "__fanest_pipes__"):
            instance = self._resolve_component(pipe)
            transformed = instance.transform(
                result,
                {"name": "data", "handler": handler, "annotation": annotation},
            )
            if inspect.isawaitable(transformed):
                transformed = await transformed
            result = transformed
        context.kwargs["data"] = result
        return result

    async def _run_interceptors(
        self,
        controller: Any,
        handler: Callable[..., Any],
        context: ExecutionContext,
        call_handler: Callable[[], Any],
    ) -> Any:
        interceptors = self._collect(controller, handler, "__fanest_interceptors__")

        async def dispatch(index: int) -> Any:
            if index >= len(interceptors):
                return await call_handler()
            instance = self._resolve_component(interceptors[index])
            result = instance.intercept(context, lambda: dispatch(index + 1))
            if inspect.isawaitable(result):
                return await result
            return result

        return await dispatch(0)

    def _bind_request_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in inspect.signature(handler).parameters.items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "request":
                bound[name] = request
        return bound

    def _bind_response_parameters(
        self, handler: Callable[..., Any], response: Response, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in inspect.signature(handler).parameters.items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "response":
                bound[name] = response
        return bound

    def _bind_background_tasks_parameters(
        self,
        handler: Callable[..., Any],
        background_tasks: FastBackgroundTasks,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in inspect.signature(handler).parameters.items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "background_tasks":
                bound[name] = background_tasks
        return bound

    def _bind_ip_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in inspect.signature(handler).parameters.items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "ip":
                bound[name] = request.client.host if request.client else None
        return bound

    def _bind_session_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in inspect.signature(handler).parameters.items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "session":
                bound[name] = request.scope.get("session", source.default)
        return bound

    def _bind_state_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in inspect.signature(handler).parameters.items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "state":
                state_name = source.name or name
                bound[name] = getattr(request.state, state_name, source.default)
        return bound

    def _bind_custom_parameters(
        self, handler: Callable[..., Any], context: ExecutionContext, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in inspect.signature(handler).parameters.items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "custom":
                factory = source.default["factory"]
                data = source.default.get("data")
                bound[name] = factory(data, context)
        return bound

    async def _run_filters(
        self,
        controller: Any,
        handler: Callable[..., Any],
        context: ExecutionContext,
        exc: Exception,
    ) -> Any:
        for exception_filter in self._collect(controller, handler, "__fanest_filters__"):
            instance = self._resolve_component(exception_filter)
            result = instance.catch(exc, context)
            if inspect.isawaitable(result):
                result = await result
            return result
        return None

    def _collect(self, controller: Any, handler: Callable[..., Any], key: str) -> list[Any]:
        global_values = {
            "__fanest_guards__": self.global_guards,
            "__fanest_pipes__": self.global_pipes,
            "__fanest_interceptors__": self.global_interceptors,
            "__fanest_filters__": self.global_filters,
        }.get(key, [])
        controller_values = getattr(controller.__class__, key, [])
        handler_values = self._metadata(handler, key, [])
        return [*global_values, *controller_values, *handler_values]

    def _response_headers(self, controller: Any, handler: Callable[..., Any]) -> list[tuple[str, str]]:
        controller_values = getattr(controller.__class__, "__fanest_response_headers__", [])
        handler_values = self._metadata(handler, "__fanest_response_headers__", [])
        return [*controller_values, *handler_values]

    def _sse_response(self, result: Any, headers: dict[str, str] | None = None) -> StreamingResponse:
        async def body():
            if hasattr(result, "__aiter__"):
                async for item in result:
                    yield self._format_sse(item)
                return
            for item in result:
                yield self._format_sse(item)

        return StreamingResponse(body(), media_type="text/event-stream", headers=headers)

    def _format_sse(self, item: Any) -> bytes:
        event = None
        data = item
        if isinstance(item, dict) and "data" in item:
            event = item.get("event")
            data = item["data"]
        payload = json.dumps(data)
        prefix = f"event: {event}\n" if event else ""
        return f"{prefix}data: {payload}\n\n".encode()

    def _resolve_component(self, component: Any) -> Any:
        if inspect.isclass(component):
            return self.container.resolve(component)
        return component

    def _metadata(self, target: Any, key: str, default: Any = None) -> Any:
        if hasattr(target, key):
            return getattr(target, key)
        func = getattr(target, "__func__", None)
        if func is not None and hasattr(func, key):
            return getattr(func, key)
        return default

    def _join_paths(self, *parts: str) -> str:
        combined = "/".join(part.strip("/") for part in parts if part.strip("/"))
        return f"/{combined}" if combined else "/"

    def _route_methods(self, method: str) -> list[str]:
        if method == "ALL":
            return ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
        return [method]
