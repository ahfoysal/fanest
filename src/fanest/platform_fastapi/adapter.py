import inspect
import json
import re
from collections.abc import Callable
from pathlib import Path as FilePath
from typing import Any

from fastapi import Body as FastBody
from fastapi import BackgroundTasks as FastBackgroundTasks
from fastapi import Cookie, FastAPI, File, Form as FastForm, Header, HTTPException, Path, Query, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.responses import Response as StarletteResponse
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
        controller_modules: dict[type, Any] | None = None,
        gateway_modules: dict[type, Any] | None = None,
    ) -> None:
        self.app = app
        self.container = container
        self.global_prefix = global_prefix
        self.global_guards = global_guards or []
        self.global_pipes = global_pipes or []
        self.global_interceptors = global_interceptors or []
        self.global_filters = global_filters or []
        self.controller_modules = controller_modules or {}
        self.gateway_modules = gateway_modules or {}
        self._parameter_cache: dict[Any, dict[str, inspect.Parameter]] = {}

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
            endpoint = self._endpoint(
                controller,
                handler.__name__,
                handler,
                module_key=self.controller_modules.get(controller),
            )
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

        module_key = self.gateway_modules.get(gateway)
        handlers_meta: dict[str, str] = {}
        for name, handler in inspect.getmembers(gateway, predicate=inspect.isfunction):
            message_metadata: MessageMetadata | None = getattr(handler, "__fanest_message__", None)
            if message_metadata is not None:
                handlers_meta[message_metadata.event] = name

        path = self._join_paths(self.global_prefix, gateway_metadata.path)

        async def websocket_endpoint(websocket: WebSocket) -> None:
            instance = await self.container.resolve_async(gateway, module_key=module_key)
            handlers = {event: getattr(instance, name) for event, name in handlers_meta.items()}
            connection_context = ExecutionContext(
                handler=getattr(instance, "on_connect", instance),
                controller=instance,
                request=websocket,
                kwargs={"websocket": websocket},
            )
            try:
                await self._run_connection_guards(instance, connection_context)
            except Exception:
                await websocket.close(code=1008)
                return
            await websocket.accept()
            self.container.resolve(WebSocketManager).connect(websocket)
            connect_hook = getattr(instance, "on_connect", None)
            if connect_hook is not None:
                result = connect_hook(websocket)
                if inspect.isawaitable(result):
                    await result
            try:
                while True:
                    try:
                        payload = await websocket.receive_json()
                    except ValueError as exc:
                        await websocket.send_json({"event": "error", "data": f"Invalid JSON payload: {exc}"})
                        continue
                    if not isinstance(payload, dict):
                        await websocket.send_json({"event": "error", "data": "Payload must be an object"})
                        continue
                    event = payload.get("event")
                    data = payload.get("data")
                    if not isinstance(event, str):
                        await websocket.send_json({"event": "error", "data": "Event must be a string"})
                        continue
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
                        context.kwargs.clear()
                        context.kwargs.update(self._bind_websocket_parameters(handler, data, websocket, context))
                    except Exception as exc:
                        handled = await self._run_filters_safe(instance, handler, context, exc)
                        await websocket.send_json(
                            {"event": "error", "data": handled if handled is not None else str(exc)}
                        )
                        continue
                    try:
                        result = handler(**context.kwargs)
                        if inspect.isawaitable(result):
                            result = await result
                    except Exception as exc:
                        handled = await self._run_filters_safe(instance, handler, context, exc)
                        await websocket.send_json(
                            {"event": "error", "data": handled if handled is not None else str(exc)}
                        )
                        continue
                    if result is not None:
                        if isinstance(result, dict) and set(result) >= {"event", "data"}:
                            await websocket.send_json({"event": result["event"], "data": result["data"]})
                            continue
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

    async def _run_connection_guards(self, gateway: Any, context: ExecutionContext) -> None:
        handler = context.handler if callable(context.handler) else None
        guards = self._collect(gateway, handler, "__fanest_guards__") if handler is not None else [
            *self.global_guards,
            *getattr(gateway.__class__, "__fanest_guards__", []),
        ]
        for guard in guards:
            instance = await self._resolve_component_async(guard, owner=gateway)
            result = instance.can_activate(context)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                raise HTTPException(status_code=403, detail="Forbidden")

    def _endpoint(
        self,
        controller_class: type,
        handler_name: str,
        handler_function: Callable[..., Any],
        module_key: Any | None = None,
    ) -> Callable[..., Any]:
        async def endpoint(
            request: Request,
            response: Response,
            background_tasks: FastBackgroundTasks,
            **kwargs: Any,
        ) -> Any:
            request_scope = self.container.begin_request()
            end_request_on_return = True
            controller = await self.container.resolve_async(controller_class, module_key=module_key)
            handler = getattr(controller, handler_name)
            context = ExecutionContext(
                handler=handler,
                controller=controller,
                request=request,
                kwargs=kwargs,
            )
            try:
                await self._run_guards(controller, handler, context)
                context.kwargs.update(self._bind_request_parameters(handler, request, kwargs))
                context.kwargs.update(self._bind_response_parameters(handler, response, kwargs))
                context.kwargs.update(
                    self._bind_native_framework_parameters(
                        handler,
                        request,
                        response,
                        background_tasks,
                        context.kwargs,
                    )
                )
                context.kwargs.update(
                    self._bind_background_tasks_parameters(
                        handler, background_tasks, context.kwargs
                    )
                )
                context.kwargs.update(self._bind_ip_parameters(handler, request, context.kwargs))
                context.kwargs.update(self._bind_session_parameters(handler, request, context.kwargs))
                context.kwargs.update(
                    self._bind_host_parameters(controller, handler, request, context.kwargs)
                )
                context.kwargs.update(self._bind_state_parameters(handler, request, kwargs))
                context.kwargs.update(self._bind_custom_parameters(handler, context, kwargs))
                kwargs = await self._run_pipes(controller, handler, context)

                async def call_handler() -> Any:
                    result = handler(**kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is None and self._uses_manual_response(handler):
                        return response
                    response_headers = dict(self._response_headers(controller, handler))
                    for name, value in response_headers.items():
                        response.headers[name] = value
                    if self._metadata(handler, "__fanest_sse__", False):
                        return self._sse_response(result, response_headers)
                    if isinstance(result, StreamableFile):
                        return result.to_response(response_headers)
                    render_template = self._metadata(handler, "__fanest_render_template__")
                    if render_template is not None:
                        return self._render_response(render_template, result, response_headers)
                    redirect = self._metadata(handler, "__fanest_redirect__")
                    if redirect is not None:
                        if isinstance(result, dict) and result.get("url"):
                            return RedirectResponse(
                                result["url"],
                                status_code=result.get("status_code", redirect["status_code"]),
                            )
                        return RedirectResponse(redirect["url"], status_code=redirect["status_code"])
                    return result

                result = await self._run_interceptors(controller, handler, context, call_handler)
                if isinstance(result, StreamingResponse):
                    request_instances = self.container.current_request_instances()
                    self.container.end_request(request_scope)
                    end_request_on_return = False
                    return self._scope_bound_streaming_response(result, request_instances)
                return result
            except Exception as exc:
                handled = await self._run_filters(controller, handler, context, exc)
                if handled is not None:
                    return handled
                raise
            finally:
                if end_request_on_return:
                    self.container.end_request(request_scope)

        endpoint.__name__ = handler_name
        endpoint.__signature__ = self._build_signature(handler_function)  # type: ignore[attr-defined]
        setattr(endpoint, "__fanest_controller_class__", controller_class)
        setattr(endpoint, "__fanest_handler_name__", handler_name)
        setattr(endpoint, "__fanest_module_key__", module_key)
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
            if name in {"request", "response", "background_tasks"}:
                continue
            source = parameter.default
            annotation = parameter.annotation
            if isinstance(source, ParameterSource):
                if source.source in {
                    "request",
                    "response",
                    "custom",
                    "ip",
                    "host",
                    "state",
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
        if source.source == "host":
            return None
        return source.default

    async def _run_guards(
        self, controller: Any, handler: Callable[..., Any], context: ExecutionContext
    ) -> None:
        for guard in self._collect(controller, handler, "__fanest_guards__"):
            instance = await self._resolve_component_async(guard, owner=controller)
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
            instance = await self._resolve_component_async(pipe, owner=controller)
            for name, value in list(kwargs.items()):
                parameter = self._parameters(handler).get(name)
                if parameter is not None and not self._should_pipe_parameter(parameter):
                    continue
                annotation = parameter.annotation if parameter is not None else None
                result = instance.transform(
                    value,
                    self._pipe_metadata(name, handler, parameter, annotation),
                )
                if inspect.isawaitable(result):
                    result = await result
                kwargs[name] = result
        for name, value in list(kwargs.items()):
            parameter = self._parameters(handler).get(name)
            if parameter is None or not isinstance(parameter.default, ParameterSource):
                continue
            annotation = parameter.annotation
            for pipe in parameter.default.pipes:
                instance = await self._resolve_component_async(pipe, owner=controller)
                result = instance.transform(
                    value,
                    self._pipe_metadata(name, handler, parameter, annotation),
                )
                if inspect.isawaitable(result):
                    result = await result
                kwargs[name] = result
        context.kwargs.update(kwargs)
        return kwargs

    def _should_pipe_parameter(self, parameter: inspect.Parameter) -> bool:
        source = parameter.default
        if not isinstance(source, ParameterSource):
            return True
        return source.source in {"body", "path", "query", "header", "cookie", "file", "files", "form"}

    def _pipe_metadata(
        self,
        name: str,
        handler: Callable[..., Any],
        parameter: inspect.Parameter | None,
        annotation: Any,
    ) -> dict[str, Any]:
        source = parameter.default if parameter is not None else None
        return {
            "name": name,
            "handler": handler,
            "annotation": annotation,
            "source": source.source if isinstance(source, ParameterSource) else None,
            "data": source.name if isinstance(source, ParameterSource) else None,
        }

    async def _run_websocket_pipes(
        self,
        gateway: Any,
        handler: Callable[..., Any],
        data: Any,
        context: ExecutionContext,
    ) -> Any:
        result = data
        parameter = self._parameters(handler).get("data")
        annotation = parameter.annotation if parameter is not None else None
        for pipe in self._collect(gateway, handler, "__fanest_pipes__"):
            instance = await self._resolve_component_async(pipe, owner=gateway)
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
        metric_counter = self._metadata(handler, "__fanest_metric_counter__")
        if metric_counter is not None:
            from fanest.metrics import MetricsRegistry

            try:
                registry = self.container.resolve(MetricsRegistry)
                registry.inc(metric_counter)
            except Exception:
                pass

        async def dispatch(index: int) -> Any:
            if index >= len(interceptors):
                return await call_handler()
            instance = await self._resolve_component_async(interceptors[index], owner=controller)
            result = instance.intercept(context, lambda: dispatch(index + 1))
            if inspect.isawaitable(result):
                return await result
            return result

        return await dispatch(0)

    def _bind_request_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "request":
                bound[name] = request
        return bound

    def _bind_response_parameters(
        self, handler: Callable[..., Any], response: Response, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "response":
                bound[name] = response
        return bound

    def _bind_native_framework_parameters(
        self,
        handler: Callable[..., Any],
        request: Request,
        response: Response,
        background_tasks: FastBackgroundTasks,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        parameters = self._parameters(handler)
        if "request" in parameters:
            bound["request"] = request
        if "response" in parameters:
            bound["response"] = response
        if "background_tasks" in parameters:
            bound["background_tasks"] = background_tasks
        return bound

    def _uses_manual_response(self, handler: Callable[..., Any]) -> bool:
        for parameter in self._parameters(handler).values():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "response":
                return not source.default.get("passthrough", False)
        return False

    def _bind_background_tasks_parameters(
        self,
        handler: Callable[..., Any],
        background_tasks: FastBackgroundTasks,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "background_tasks":
                bound[name] = background_tasks
        return bound

    def _bind_ip_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "ip":
                bound[name] = request.client.host if request.client else None
        return bound

    def _bind_host_parameters(
        self, controller: Any, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        hostname = request.url.hostname
        parts = hostname.split(".") if hostname else []
        host_params = self._host_params(controller, hostname)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "host":
                if source.name is None:
                    bound[name] = hostname
                elif source.name in host_params:
                    bound[name] = host_params[source.name]
                elif source.name.isdigit():
                    bound[name] = parts[int(source.name)] if int(source.name) < len(parts) else source.default
                else:
                    bound[name] = source.default
        return bound

    def _host_params(self, controller: Any, hostname: str | None) -> dict[str, str]:
        metadata: ControllerMetadata | None = getattr(controller.__class__, "__fanest_controller__", None)
        if metadata is None or metadata.host is None or hostname is None:
            return {}
        pattern_parts = metadata.host.split(".")
        host_parts = hostname.split(".")
        if len(pattern_parts) != len(host_parts):
            return {}
        params: dict[str, str] = {}
        for pattern, value in zip(pattern_parts, host_parts, strict=True):
            if (pattern.startswith(":") and len(pattern) > 1):
                params[pattern[1:]] = value
            elif pattern.startswith("{") and pattern.endswith("}"):
                params[pattern[1:-1]] = value
            elif pattern != value:
                return {}
        return params

    def _bind_session_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "session":
                bound[name] = request.scope.get("session", source.default)
        return bound

    def _bind_state_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "state":
                state_name = source.name or name
                bound[name] = getattr(request.state, state_name, source.default)
        return bound

    def _bind_custom_parameters(
        self, handler: Callable[..., Any], context: ExecutionContext, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "custom":
                factory = source.default["factory"]
                data = source.default.get("data")
                bound[name] = factory(data, context)
        return bound

    def _bind_websocket_parameters(
        self,
        handler: Callable[..., Any],
        data: Any,
        websocket: WebSocket,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource):
                if source.source == "message_body":
                    bound[name] = self._select_message_body(data, source)
                elif source.source == "connected_socket":
                    bound[name] = websocket
                elif source.source == "custom":
                    factory = source.default["factory"]
                    custom_data = source.default.get("data")
                    bound[name] = factory(custom_data, context)
                continue
            if name == "data":
                bound[name] = data
            elif name == "websocket":
                bound[name] = websocket
        return bound

    def _select_message_body(self, data: Any, source: ParameterSource) -> Any:
        if source.name is None:
            return data
        if isinstance(data, dict):
            return data.get(source.name, source.default)
        return source.default

    async def _run_filters(
        self,
        controller: Any,
        handler: Callable[..., Any],
        context: ExecutionContext,
        exc: Exception,
    ) -> Any:
        for exception_filter in self._collect(controller, handler, "__fanest_filters__"):
            instance = await self._resolve_component_async(exception_filter, owner=controller)
            catch_types = getattr(instance.__class__, "__fanest_catch_exceptions__", (Exception,))
            if not isinstance(exc, catch_types):
                continue
            result = instance.catch(exc, context)
            if inspect.isawaitable(result):
                result = await result
            return result
        return None

    async def _run_filters_safe(
        self,
        controller: Any,
        handler: Callable[..., Any],
        context: ExecutionContext,
        exc: Exception,
    ) -> Any:
        try:
            handled = await self._run_filters(controller, handler, context, exc)
            return self._websocket_filter_payload(handled)
        except Exception as filter_exc:
            return str(filter_exc)

    def _websocket_filter_payload(self, handled: Any) -> Any:
        if not isinstance(handled, StarletteResponse):
            return handled
        body = getattr(handled, "body", b"")
        if isinstance(body, memoryview):
            body = body.tobytes()
        if isinstance(body, bytes):
            text = body.decode(getattr(handled, "charset", "utf-8") or "utf-8")
        else:
            text = str(body)
        content_type = handled.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

    async def handle_validation_error(self, request: Request, exc: RequestValidationError) -> Any:
        route = request.scope.get("route")
        endpoint = getattr(route, "endpoint", None)
        controller_class = getattr(endpoint, "__fanest_controller_class__", None)
        handler_name = getattr(endpoint, "__fanest_handler_name__", None)
        module_key = getattr(endpoint, "__fanest_module_key__", None)
        if controller_class is None or handler_name is None:
            return None
        request_scope = self.container.begin_request()
        try:
            controller = await self.container.resolve_async(controller_class, module_key=module_key)
            handler = getattr(controller, handler_name)
            context = ExecutionContext(handler=handler, controller=controller, request=request, kwargs={})
            return await self._run_filters(controller, handler, context, exc)
        finally:
            self.container.end_request(request_scope)

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

    def _scope_bound_streaming_response(
        self,
        response: StreamingResponse,
        request_instances: dict[Any, Any] | None,
    ) -> StreamingResponse:
        original_iterator = response.body_iterator
        container = self.container

        async def body_iterator():
            stream_scope = container.bind_request_instances(request_instances)
            try:
                async for chunk in original_iterator:
                    yield chunk
            finally:
                container.end_request(stream_scope)

        response.body_iterator = body_iterator()
        return response

    def _format_sse(self, item: Any) -> bytes:
        event = None
        data = item
        if isinstance(item, dict) and "data" in item:
            event = item.get("event")
            data = item["data"]
        payload = json.dumps(data)
        prefix = f"event: {event}\n" if event else ""
        return f"{prefix}data: {payload}\n\n".encode()

    def _render_response(
        self,
        template: str,
        context: dict[str, Any] | None,
        headers: dict[str, str] | None = None,
    ) -> HTMLResponse:
        path = FilePath(template)
        content = path.read_text(encoding="utf-8") if path.exists() else template
        values = context or {}

        def replace(match: re.Match[str]) -> str:
            return str(values.get(match.group("key"), ""))

        rendered = re.sub(r"{{\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*}}", replace, content)
        return HTMLResponse(rendered, headers=headers)

    def _resolve_component(self, component: Any, *, owner: Any | None = None) -> Any:
        if inspect.isclass(component):
            module_key = None
            if owner is not None:
                module_key = self.controller_modules.get(owner.__class__) or self.gateway_modules.get(
                    owner.__class__
                )
            return self.container.resolve(component, module_key=module_key)
        return component

    async def _resolve_component_async(self, component: Any, *, owner: Any | None = None) -> Any:
        if inspect.isclass(component):
            module_key = None
            if owner is not None:
                module_key = self.controller_modules.get(owner.__class__) or self.gateway_modules.get(
                    owner.__class__
                )
            return await self.container.resolve_async(component, module_key=module_key)
        return component

    def _parameters(self, handler: Callable[..., Any]) -> dict[str, inspect.Parameter]:
        key = getattr(handler, "__func__", handler)
        cached = self._parameter_cache.get(key)
        if cached is not None:
            return cached
        parameters = dict(inspect.signature(handler).parameters)
        self._parameter_cache[key] = parameters
        return parameters

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
