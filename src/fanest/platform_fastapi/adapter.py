import base64
import inspect
import json
import re
from copy import deepcopy
from collections.abc import Callable
from pathlib import Path as FilePath
from typing import Any, cast

from fastapi import Body as FastBody
from fastapi import BackgroundTasks as FastBackgroundTasks
from fastapi import Cookie, FastAPI, File, Form as FastForm, Header, HTTPException, Path, Query, Request, Response, UploadFile, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTasks as StarletteBackgroundTasks
from starlette.responses import Response as StarletteResponse
from starlette.websockets import WebSocketDisconnect

from fanest.common.pipes import DefaultValuePipe
from fanest.common.responses import StreamableFile
from fanest.common.upload import (
    AnyFilesInterceptor,
    FileFieldsInterceptor,
    FileInterceptor,
    FilesInterceptor,
)
from fanest.common.versioning import VERSION_NEUTRAL, VersioningOptions, VersioningType
from fanest.core.container import FaNestContainer
from fanest.core.metadata import (
    ControllerMetadata,
    ExecutionContext,
    GatewayMetadata,
    MessageMetadata,
    ParameterSource,
    RouteMetadata,
)
from fanest.websockets import UnsupportedSocketIoProtocolError, WebSocketManager, WsException, WsResponse


class _WebSocketAckCallback:
    def __init__(
        self,
        adapter: "FastApiAdapter",
        websocket: WebSocket,
        ack_id: Any,
        event: str,
        namespace: str,
    ) -> None:
        self.adapter = adapter
        self.websocket = websocket
        self.ack_id = ack_id
        self.event = event
        self.namespace = namespace
        self.sent = False

    async def __call__(self, data: Any = None) -> bool:
        if self.ack_id is None:
            self.sent = True
            return False
        self.sent = await self.adapter._send_websocket_ack(
            self.websocket,
            self.ack_id,
            self.event,
            data,
            namespace=self.namespace,
        )
        return self.sent


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
        versioning: VersioningOptions | None = None,
        controller_modules: dict[type, Any] | None = None,
        gateway_modules: dict[type, Any] | None = None,
    ) -> None:
        self.app = app
        self.container = container
        self.global_prefix = global_prefix
        self.global_guards = global_guards if global_guards is not None else []
        self.global_pipes = global_pipes if global_pipes is not None else []
        self.global_interceptors = global_interceptors if global_interceptors is not None else []
        self.global_filters = global_filters if global_filters is not None else []
        self.versioning = versioning
        self.controller_modules = controller_modules or {}
        self.gateway_modules = gateway_modules or {}
        self._parameter_cache: dict[Any, dict[str, inspect.Parameter]] = {}
        self._non_uri_versioned_routes: set[tuple[str, str]] = set()

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
        if getattr(controller, "__fanest_swagger_exclude_controller__", False):
            return

        routes: list[tuple[str, Callable[..., Any], RouteMetadata]] = []
        for _, handler in inspect.getmembers(controller, predicate=inspect.isfunction):
            route_metadata: RouteMetadata | None = getattr(handler, "__fanest_route__", None)
            if route_metadata is None:
                continue
            routes.append((route_metadata.path, handler, route_metadata))

        for _, handler, route_metadata in sorted(routes, key=self._route_sort_key):
            route_version = self._metadata(handler, "__fanest_version__") or getattr(
                controller, "__fanest_version__", None
            )
            versioning = self.versioning or (VersioningOptions() if route_version else None)
            versions = self._route_versions(route_version, versioning)
            path_versions = (
                (None,)
                if versioning is not None and versioning.type != VersioningType.URI
                else versions
            )
            endpoint = self._endpoint(
                controller,
                handler.__name__,
                handler,
                route_version=route_version,
                versioning=versioning,
                module_key=self.controller_modules.get(controller),
            )
            route_options = dict(route_metadata.options)
            tags = getattr(controller, "__fanest_swagger_tags__", None)
            if tags and "tags" not in route_options:
                route_options["tags"] = tags
            controller_responses = getattr(controller, "__fanest_pending_responses__", None)
            if controller_responses:
                route_options["responses"] = {
                    **controller_responses,
                    **dict(route_options.get("responses", {})),
                }
            pending_responses = getattr(handler, "__fanest_pending_responses__", None)
            if pending_responses:
                route_options["responses"] = {
                    **dict(route_options.get("responses", {})),
                    **pending_responses,
                }
            pending_route_options = getattr(handler, "__fanest_pending_route_options__", None)
            if pending_route_options:
                route_options.update(pending_route_options)
            pending_openapi_extra = getattr(handler, "__fanest_pending_openapi_extra__", None)
            controller_openapi_extra = getattr(controller, "__fanest_pending_openapi_extra__", None)
            if controller_openapi_extra or pending_openapi_extra:
                route_options["openapi_extra"] = self._merge_openapi_extras(
                    deepcopy(controller_openapi_extra or {}),
                    deepcopy(route_options.get("openapi_extra", {})),
                    deepcopy(pending_openapi_extra or {}),
                )
            upload_openapi_extra = self._upload_openapi_extra(controller, handler)
            if upload_openapi_extra:
                route_options["openapi_extra"] = self._merge_openapi_extras(
                    deepcopy(upload_openapi_extra),
                    deepcopy(route_options.get("openapi_extra", {})),
                )
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
            for version in path_versions:
                path = self._versioned_path(
                    controller_metadata.prefix,
                    route_metadata.path,
                    version,
                    versioning,
                )
                methods = self._route_methods(route_metadata.method)
                self._ensure_supported_versioned_route(path, methods, versioning, route_version)
                self.app.add_api_route(
                    path,
                    endpoint,
                    methods=methods,
                    **route_options,
                )

    def _route_sort_key(self, route: tuple[str, Callable[..., Any], RouteMetadata]) -> tuple[int, int]:
        path = route[0]
        dynamic_segments = path.count("{")
        return (dynamic_segments, -len(path))

    def _route_versions(
        self,
        route_version: Any,
        versioning: VersioningOptions | None,
    ) -> tuple[str | None, ...]:
        if route_version is None:
            return (versioning.default_version,) if versioning and versioning.default_version else (None,)
        if route_version == VERSION_NEUTRAL:
            return (None,)
        if isinstance(route_version, list | tuple | set):
            return tuple(str(version) for version in route_version)
        return (str(route_version),)

    def _versioned_path(
        self,
        controller_prefix: str,
        route_path: str,
        version: str | None,
        versioning: VersioningOptions | None,
    ) -> str:
        version_prefix = ""
        if version and (versioning is None or versioning.type == VersioningType.URI):
            prefix = versioning.prefix if versioning is not None else "v"
            version_prefix = f"{prefix}{version}" if prefix else version
        return self._join_paths(
            self.global_prefix,
            version_prefix,
            controller_prefix,
            route_path,
        )

    def _ensure_supported_versioned_route(
        self,
        path: str,
        methods: list[str],
        versioning: VersioningOptions | None,
        route_version: Any,
    ) -> None:
        if route_version is None or versioning is None or versioning.type == VersioningType.URI:
            return
        for method in methods:
            key = (method, path)
            if key in self._non_uri_versioned_routes:
                raise RuntimeError(
                    "Header, media-type, and custom versioning do not yet support "
                    f"multiple handlers for {method} {path}."
                )
            self._non_uri_versioned_routes.add(key)

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
            handlers: dict[str, Callable[..., Any]] = {
                event: cast(Callable[..., Any], getattr(instance, name))
                for event, name in handlers_meta.items()
            }
            connection_context = ExecutionContext(
                handler=getattr(instance, "on_connect", instance),
                controller=instance,
                request=websocket,
                kwargs={"websocket": websocket},
            )
            try:
                await self._run_connection_guards(instance, connection_context)
            except Exception:
                await self._close_websocket_safely(websocket, code=1008)
                return
            try:
                await websocket.accept()
            except (RuntimeError, WebSocketDisconnect):
                return
            engineio_error = self._engineio_transport_error(websocket)
            if engineio_error is not None:
                await self._send_websocket_event(websocket, "error", engineio_error)
                await self._close_websocket_safely(websocket, code=1003)
                return
            websocket_manager = self.container.resolve(WebSocketManager)
            websocket_manager.connect(websocket)
            await websocket_manager.emit_lifecycle("connect", websocket)
            connect_hook = getattr(instance, "on_connect", None)
            if connect_hook is not None:
                try:
                    result = connect_hook(websocket)
                    if inspect.isawaitable(result):
                        await result
                except WebSocketDisconnect:
                    self.container.resolve(WebSocketManager).disconnect(websocket)
                    await self._run_websocket_disconnect_hook(instance, websocket)
                    return
                except Exception as exc:
                    handled = await self._run_filters_safe(
                        instance,
                        connect_hook,
                        connection_context,
                        exc,
                    )
                    await self._send_websocket_event(
                        websocket,
                        "error",
                        handled if handled is not None else str(exc),
                    )
                    self.container.resolve(WebSocketManager).disconnect(websocket)
                    await self._run_websocket_disconnect_hook(instance, websocket)
                    await self._close_websocket_safely(websocket, code=1011)
                    return
            try:
                while True:
                    try:
                        payload = await self._receive_websocket_payload(websocket)
                    except UnsupportedSocketIoProtocolError as exc:
                        if not await self._send_websocket_event(websocket, "error", str(exc)):
                            break
                        continue
                    except ValueError as exc:
                        if not await self._send_websocket_event(
                            websocket,
                            "error",
                            f"Invalid JSON payload: {exc}",
                        ):
                            break
                        continue
                    if not isinstance(payload, dict):
                        if not await self._send_websocket_event(
                            websocket,
                            "error",
                            "Payload must be an object",
                        ):
                            break
                        continue
                    event = payload.get("event")
                    data = payload.get("data")
                    if not isinstance(event, str):
                        if not await self._send_websocket_event(
                            websocket,
                            "error",
                            "Event must be a string",
                        ):
                            break
                        continue
                    try:
                        namespace = self._websocket_namespace(payload)
                    except ValueError as exc:
                        if not await self._send_websocket_event(websocket, "error", str(exc)):
                            break
                        continue
                    if not self.container.resolve(WebSocketManager).in_namespace(websocket, namespace):
                        if not await self._send_websocket_event(
                            websocket,
                            "error",
                            f"Socket is not connected to namespace {namespace}",
                            namespace=namespace,
                        ):
                            break
                        continue
                    handler = handlers.get(event)
                    if handler is None:
                        if not await self._send_websocket_event(
                            websocket,
                            "error",
                            "Unknown event",
                            namespace=namespace,
                        ):
                            break
                        continue
                    handler_callable = handler
                    ack_callback = None
                    context = ExecutionContext(
                        handler=handler_callable,
                        controller=instance,
                        request=websocket,
                        kwargs={"data": data, "websocket": websocket, "namespace": namespace},
                    )
                    try:
                        await self._run_guards(instance, handler_callable, context)
                        data = await self._run_websocket_pipes(instance, handler_callable, data, context)
                        context.kwargs.clear()
                        context.kwargs.update(
                            await self._bind_websocket_parameters(
                                handler_callable,
                                data,
                                websocket,
                                context,
                                event=event,
                                namespace=namespace,
                                ack_id=self._websocket_ack_id(payload),
                            )
                        )
                        ack_callback = context.kwargs.pop("__fanest_ack_callback__", None)
                    except Exception as exc:
                        handled = await self._run_filters_safe(instance, handler_callable, context, exc)
                        if not await self._send_websocket_event(
                            websocket,
                            "error",
                            handled if handled is not None else str(exc),
                        ):
                            break
                        continue
                    try:
                        async def call_handler() -> Any:
                            handler_result = handler_callable(**context.kwargs)
                            if inspect.isawaitable(handler_result):
                                handler_result = await handler_result
                            return handler_result

                        result = await self._run_interceptors(
                            instance,
                            handler_callable,
                            context,
                            call_handler,
                        )
                    except Exception as exc:
                        handled = await self._run_filters_safe(instance, handler, context, exc)
                        if not await self._send_websocket_event(
                            websocket,
                            "error",
                            handled if handled is not None else str(exc),
                        ):
                            break
                        continue
                    if ack_callback is not None and getattr(ack_callback, "sent", False):
                        continue
                    if ack_callback is not None and result is None:
                        continue
                    if result is not None:
                        if self._is_websocket_response_sequence(result):
                            for item in result:
                                if not await self._send_websocket_response_item(
                                    websocket,
                                    item,
                                    namespace=namespace,
                                ):
                                    break
                            continue
                        if isinstance(result, WsResponse):
                            if not await self._send_websocket_response_item(
                                websocket,
                                result,
                                namespace=namespace,
                            ):
                                break
                            continue
                        if isinstance(result, dict) and set(result) >= {"event", "data"}:
                            if not await self._send_websocket_event(
                                websocket,
                                result["event"],
                                result["data"],
                                namespace=namespace,
                            ):
                                break
                            continue
                        ack_id = self._websocket_ack_id(payload)
                        if ack_id is not None:
                            if not await self._send_websocket_ack(
                                websocket,
                                ack_id,
                                event,
                                result,
                                namespace=namespace,
                            ):
                                break
                            continue
                        if not await self._send_websocket_event(websocket, event, result, namespace=namespace):
                            break
                    else:
                        ack_id = self._websocket_ack_id(payload)
                        if ack_id is not None and not await self._send_websocket_ack(
                            websocket,
                            ack_id,
                            event,
                            None,
                            namespace=namespace,
                        ):
                            break
            except WebSocketDisconnect:
                pass
            finally:
                websocket_manager = self.container.resolve(WebSocketManager)
                # Connect is emitted exactly once (for the default namespace) on
                # connection, so disconnect must fire exactly once per socket to keep
                # the lifecycle symmetric and presence counters balanced.
                await websocket_manager.emit_lifecycle("disconnect", websocket)
                websocket_manager.disconnect(websocket)
                await self._run_websocket_disconnect_hook(instance, websocket)

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
        route_version: Any = None,
        versioning: VersioningOptions | None = None,
        module_key: Any | None = None,
    ) -> Callable[..., Any]:
        async def endpoint(
            request: Request,
            response: Response,
            background_tasks: FastBackgroundTasks,
            **kwargs: Any,
        ) -> Any:
            kwargs = self._restore_reserved_user_parameter_names(handler_function, kwargs)
            request_scope = self.container.begin_request()
            end_request_on_return = True
            self._attach_raw_body(request)
            controller = await self.container.resolve_async(controller_class, module_key=module_key)
            handler = getattr(controller, handler_name)
            context = ExecutionContext(
                handler=handler,
                controller=controller,
                request=request,
                kwargs=kwargs,
            )
            try:
                self._ensure_request_version(request, route_version, versioning)
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
                context.kwargs.update(self._bind_header_parameters(handler, request, context.kwargs))
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
                context.kwargs.update(await self._bind_custom_parameters(handler, context, kwargs))
                kwargs = await self._run_pipes(controller, handler, context)

                async def call_handler() -> Any:
                    result = handler(**kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is None and self._uses_manual_response(handler):
                        return response
                    response_headers = self._response_headers(controller, handler)
                    self._append_response_headers(response, response_headers)
                    if self._metadata(handler, "__fanest_sse__", False):
                        return self._with_response_headers(self._sse_response(result), response_headers)
                    if isinstance(result, StreamableFile):
                        return self._with_response_headers(result.to_response(), response_headers)
                    render_template = self._metadata(handler, "__fanest_render_template__")
                    if render_template is not None:
                        return self._with_response_headers(
                            self._render_response(render_template, result),
                            response_headers,
                        )
                    redirect = self._metadata(handler, "__fanest_redirect__")
                    if redirect is not None:
                        if isinstance(result, dict) and result.get("url"):
                            return self._with_response_headers(
                                RedirectResponse(
                                    result["url"],
                                    status_code=result.get("status_code", redirect["status_code"]),
                                ),
                                response_headers,
                            )
                        return self._with_response_headers(
                            RedirectResponse(redirect["url"], status_code=redirect["status_code"]),
                            response_headers,
                        )
                    content_type_override = next(
                        (value for name, value in response_headers if name.lower() == "content-type"),
                        None,
                    )
                    if content_type_override is not None and not isinstance(result, StarletteResponse):
                        # FastAPI merges the injected sub-response headers into the
                        # serialized JSONResponse by *appending*, which duplicates a
                        # singular header like Content-Type. Build the response here so
                        # the explicit @SetHeader value overrides FastAPI's default.
                        built = JSONResponse(
                            jsonable_encoder(result),
                            status_code=response.status_code or self._route_status_code(handler_function),
                        )
                        return self._with_response_headers(built, response_headers)
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
                    self._attach_background_tasks(handled, background_tasks)
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

    def _ensure_request_version(
        self,
        request: Request,
        route_version: Any,
        versioning: VersioningOptions | None,
    ) -> None:
        if route_version is None or versioning is None or versioning.type == VersioningType.URI:
            return
        if route_version == VERSION_NEUTRAL:
            return
        route_versions = {str(version) for version in self._route_versions(route_version, versioning) if version}
        if not route_versions:
            return
        request_versions = set(self._extract_request_versions(request, versioning))
        if request_versions & route_versions:
            return
        raise HTTPException(status_code=404, detail="Version not found")

    def _extract_request_versions(self, request: Request, versioning: VersioningOptions) -> list[str]:
        if versioning.type == VersioningType.HEADER:
            value = request.headers.get(versioning.header)
            return [value] if value else []
        if versioning.type == VersioningType.MEDIA_TYPE:
            accept = request.headers.get("accept", "")
            pattern = rf"(?:^|[;\s,]){re.escape(versioning.key)}=(?P<version>[^;\s,]+)"
            return [match.group("version") for match in re.finditer(pattern, accept)]
        if versioning.type == VersioningType.CUSTOM:
            if versioning.extractor is None:
                raise RuntimeError("Custom versioning requires an extractor.")
            value = versioning.extractor(request)
            if value is None:
                return []
            if isinstance(value, str):
                return [value]
            return [str(item) for item in value]
        return []

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
            if self._is_native_framework_parameter(name, parameter):
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
                annotation = self._file_annotation(source, annotation)
            else:
                default = parameter.default
            parameter_name = name
            parameters.append(
                inspect.Parameter(
                    self._signature_parameter_name(name, parameter),
                    inspect.Parameter.KEYWORD_ONLY,
                    default=self._signature_parameter_default(parameter_name, parameter, default),
                    annotation=annotation,
                )
            )
        return inspect.Signature(parameters=parameters, return_annotation=original.return_annotation)

    def _file_annotation(self, source: ParameterSource, annotation: Any) -> Any:
        if annotation is not inspect.Parameter.empty:
            return annotation
        if source.source == "file":
            return UploadFile
        if source.source == "files":
            return list[UploadFile]
        return annotation

    def _signature_parameter_name(self, name: str, parameter: inspect.Parameter) -> str:
        if name in {"request", "response", "background_tasks"} and not self._is_native_framework_parameter(name, parameter):
            return f"__fanest_user_{name}"
        return name

    def _signature_parameter_default(
        self,
        name: str,
        parameter: inspect.Parameter,
        default: Any,
    ) -> Any:
        if self._signature_parameter_name(name, parameter) == name:
            return default
        source = parameter.default
        if isinstance(source, ParameterSource):
            return self._fastapi_default(
                ParameterSource(
                    source=source.source,
                    name=source.name or name,
                    default=source.default,
                    pipes=source.pipes,
                ),
                name,
            )
        if default is inspect.Parameter.empty:
            return Query(..., alias=name)
        return default

    def _restore_reserved_user_parameter_names(
        self,
        handler: Callable[..., Any],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        restored = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            internal_name = self._signature_parameter_name(name, parameter)
            if internal_name != name and internal_name in restored:
                restored[name] = restored.pop(internal_name)
        return restored

    def _is_native_framework_parameter(self, name: str, parameter: inspect.Parameter) -> bool:
        if name not in {"request", "response", "background_tasks"}:
            return False
        if isinstance(parameter.default, ParameterSource):
            return False
        return parameter.annotation in {Request, Response, FastBackgroundTasks}

    def _source_default(self, source: ParameterSource) -> Any:
        # NestJS-style: a `DefaultValuePipe` makes the parameter optional and
        # supplies its default, so callers don't also have to pass `default=...`.
        default = source.default
        if default is ... or default is inspect.Parameter.empty:
            for pipe in source.pipes or ():
                if isinstance(pipe, DefaultValuePipe):
                    return pipe.default
        return default

    def _fastapi_default(self, source: ParameterSource, fallback_name: str) -> Any:
        alias = source.name or fallback_name
        default = self._source_default(source)
        if source.source == "body":
            return FastBody(default, alias=source.name)
        if source.source == "path":
            return Path(source.default, alias=alias)
        if source.source == "query":
            return Query(default, alias=source.name)
        if source.source == "header":
            return Header(default, alias=source.name)
        if source.source == "cookie":
            return Cookie(default, alias=source.name)
        if source.source == "file":
            return File(default, alias=source.name)
        if source.source == "files":
            return File(default, alias=source.name)
        if source.source == "form":
            return FastForm(default, alias=source.name)
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
                if parameter is not None and self._is_native_framework_parameter(name, parameter):
                    continue
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
        if annotation is inspect.Parameter.empty:
            annotation = Any
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
                registry.inc(
                    metric_counter,
                    labels=self._metadata(handler, "__fanest_metric_counter_labels__", {}),
                )
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

    def _attach_raw_body(self, request: Request) -> None:
        if "fanest.raw_body" not in request.scope:
            return
        raw_body = request.scope["fanest.raw_body"]
        request.state.raw_body = raw_body
        setattr(request, "raw_body", raw_body)

    def _bind_header_parameters(
        self, handler: Callable[..., Any], request: Request, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "header" and source.name is None:
                bound[name] = dict(request.headers)
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
        request_parameter = parameters.get("request")
        if request_parameter is not None and self._is_native_framework_parameter("request", request_parameter):
            bound["request"] = request
        response_parameter = parameters.get("response")
        if response_parameter is not None and self._is_native_framework_parameter("response", response_parameter):
            bound["response"] = response
        background_tasks_parameter = parameters.get("background_tasks")
        if (
            background_tasks_parameter is not None
            and self._is_native_framework_parameter("background_tasks", background_tasks_parameter)
        ):
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

    async def _bind_custom_parameters(
        self, handler: Callable[..., Any], context: ExecutionContext, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        bound = dict(kwargs)
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource) and source.source == "custom":
                factory = source.default["factory"]
                data = source.default.get("data")
                result = factory(data, context)
                if inspect.isawaitable(result):
                    result = await result
                bound[name] = result
        return bound

    async def _bind_websocket_parameters(
        self,
        handler: Callable[..., Any],
        data: Any,
        websocket: WebSocket,
        context: ExecutionContext,
        *,
        event: str,
        namespace: str = "/",
        ack_id: Any = None,
    ) -> dict[str, Any]:
        bound: dict[str, Any] = {}
        for name, parameter in self._parameters(handler).items():
            source = parameter.default
            if isinstance(source, ParameterSource):
                if source.source == "message_body":
                    bound[name] = self._select_message_body(data, source)
                elif source.source == "connected_socket":
                    bound[name] = websocket
                elif source.source == "ack":
                    ack_callback = _WebSocketAckCallback(
                        self,
                        websocket,
                        ack_id,
                        event,
                        namespace,
                    )
                    bound[name] = ack_callback
                    bound["__fanest_ack_callback__"] = ack_callback
                elif source.source == "custom":
                    factory = source.default["factory"]
                    custom_data = source.default.get("data")
                    value = factory(custom_data, context)
                    if inspect.isawaitable(value):
                        value = await value
                    bound[name] = value
                continue
            if name == "data":
                bound[name] = data
            elif name == "websocket":
                bound[name] = websocket
            elif name == "namespace":
                bound[name] = namespace
        return bound

    async def _run_websocket_disconnect_hook(self, instance: Any, websocket: WebSocket) -> None:
        disconnect_hook = getattr(instance, "on_disconnect", None)
        if disconnect_hook is None:
            return
        try:
            result = disconnect_hook(websocket)
            if inspect.isawaitable(result):
                await result
        except Exception:
            return

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

    def _attach_background_tasks(self, response: Any, background_tasks: FastBackgroundTasks) -> None:
        if not isinstance(response, StarletteResponse):
            return
        if not background_tasks.tasks:
            return
        if response.background is None:
            response.background = background_tasks
            return
        existing_tasks = getattr(response.background, "tasks", None)
        if isinstance(existing_tasks, list):
            existing_tasks.extend(background_tasks.tasks)
            return
        combined = StarletteBackgroundTasks()
        combined.tasks.append(response.background)
        combined.tasks.extend(background_tasks.tasks)
        response.background = combined

    async def _run_filters_safe(
        self,
        controller: Any,
        handler: Callable[..., Any],
        context: ExecutionContext,
        exc: Exception,
    ) -> Any:
        try:
            handled = await self._run_filters(controller, handler, context, exc)
            if handled is None and isinstance(exc, WsException):
                return self._websocket_payload(exc.get_error())
            return self._websocket_filter_payload(handled)
        except Exception as filter_exc:
            return str(filter_exc)

    def _websocket_filter_payload(self, handled: Any) -> Any:
        return self._websocket_payload(handled)

    def _is_websocket_response_sequence(self, result: Any) -> bool:
        return isinstance(result, list | tuple) and all(self._is_websocket_response_item(item) for item in result)

    def _is_websocket_response_item(self, result: Any) -> bool:
        return isinstance(result, WsResponse) or (
            isinstance(result, dict)
            and isinstance(result.get("event"), str)
            and "data" in result
        )

    async def _send_websocket_response_item(
        self,
        websocket: WebSocket,
        result: Any,
        *,
        namespace: str = "/",
    ) -> bool:
        if isinstance(result, WsResponse):
            return await self._send_websocket_event(
                websocket,
                result.event,
                result.data,
                namespace=namespace,
            )
        return await self._send_websocket_event(
            websocket,
            result["event"],
            result["data"],
            namespace=namespace,
        )

    def _websocket_payload(self, handled: Any) -> Any:
        if not isinstance(handled, StarletteResponse):
            if isinstance(handled, memoryview):
                handled = handled.tobytes()
            if isinstance(handled, bytes):
                return self._websocket_binary_payload(handled)
            if isinstance(handled, dict):
                return {str(key): self._websocket_payload(value) for key, value in handled.items()}
            if isinstance(handled, list | tuple):
                return [self._websocket_payload(value) for value in handled]
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

    def _websocket_binary_payload(self, data: bytes) -> dict[str, str]:
        return {
            "__fanest_binary__": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        }

    async def _receive_websocket_payload(self, websocket: WebSocket) -> Any:
        message = await websocket.receive()
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        text = message.get("text")
        if text is not None:
            self._ensure_not_socketio_engine_frame(text)
            return json.loads(text)
        data = message.get("bytes")
        if data is not None:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Binary websocket frames must contain UTF-8 JSON envelopes") from exc
            self._ensure_not_socketio_engine_frame(text)
            return json.loads(text)
        raise ValueError("Unsupported websocket frame")

    def _ensure_not_socketio_engine_frame(self, text: str) -> None:
        stripped = text.strip()
        if stripped[:1] in {"0", "1", "2", "3", "4", "5", "6"}:
            raise UnsupportedSocketIoProtocolError(
                "Native Socket.IO/Engine.IO frames are not supported by this gateway. "
                "Use FaNest JSON websocket envelopes like {'event': 'name', 'data': ...}."
            )

    def _engineio_transport_error(self, websocket: WebSocket) -> str | None:
        transport = websocket.query_params.get("transport")
        if transport is not None and transport != "websocket":
            return (
                f"Unsupported Engine.IO transport mode '{transport}'. "
                "FaNest websocket gateways only support direct WebSocket connections with JSON envelopes."
            )
        if "EIO" in websocket.query_params:
            return (
                "Native Engine.IO handshakes are not supported by this gateway. "
                "Use direct WebSocket connections with FaNest JSON envelopes."
            )
        return None

    def _websocket_ack_id(self, payload: dict[str, Any]) -> Any:
        ack_id = payload.get("ack", payload.get("id"))
        if isinstance(ack_id, bool):
            return None
        return ack_id

    def _websocket_namespace(self, payload: dict[str, Any]) -> str:
        namespace = payload.get("namespace", payload.get("nsp", "/"))
        if not isinstance(namespace, str):
            raise ValueError("Namespace must be a string")
        stripped = namespace.strip() or "/"
        return stripped if stripped.startswith("/") else f"/{stripped}"

    async def _send_websocket_ack(
        self,
        websocket: WebSocket,
        ack_id: Any,
        event: str,
        data: Any,
        *,
        namespace: str = "/",
    ) -> bool:
        return await self._send_websocket_event(
            websocket,
            "ack",
            {"id": ack_id, "event": event, "data": self._websocket_payload(data)},
            namespace=namespace,
        )

    async def _send_websocket_event(
        self,
        websocket: WebSocket,
        event: str,
        data: Any,
        *,
        namespace: str = "/",
    ) -> bool:
        payload = {"event": event, "data": self._websocket_payload(data)}
        if namespace != "/":
            payload["namespace"] = namespace
        try:
            await websocket.send_json(payload)
            return True
        except TypeError:
            try:
                payload["data"] = str(data)
                await websocket.send_json(payload)
                return True
            except (RuntimeError, TypeError, WebSocketDisconnect):
                self.container.resolve(WebSocketManager).disconnect(websocket)
                return False
        except (RuntimeError, WebSocketDisconnect):
            self.container.resolve(WebSocketManager).disconnect(websocket)
            return False

    async def _close_websocket_safely(self, websocket: WebSocket, *, code: int) -> None:
        try:
            await websocket.close(code=code)
        except (RuntimeError, WebSocketDisconnect):
            return

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
        if key == "__fanest_filters__":
            return [*handler_values, *controller_values, *global_values]
        return [*global_values, *controller_values, *handler_values]

    def _merge_openapi_extras(self, *extras: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for extra in extras:
            self._deep_merge_openapi_extra(merged, extra)
        return merged

    def _deep_merge_openapi_extra(self, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if key == "schema":
                target[key] = value
                continue
            if key == "parameters":
                target[key] = [*target.get(key, []), *value]
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_merge_openapi_extra(target[key], value)
                continue
            target[key] = value

    def _upload_openapi_extra(
        self,
        controller: type,
        handler: Callable[..., Any],
    ) -> dict[str, Any] | None:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, parameter in inspect.signature(handler).parameters.items():
            if name == "self":
                continue
            source = parameter.default
            if not isinstance(source, ParameterSource) or source.source not in {"file", "files"}:
                continue
            field_name = source.name or name
            schema = self._upload_property_schema(source.source)
            max_items = self._upload_max_items(controller, handler, field_name, source.source)
            if max_items is not None and schema.get("type") == "array":
                schema["maxItems"] = max_items
            properties[field_name] = schema
            if self._source_default(source) is ...:
                required.append(field_name)
        if not properties:
            return None
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return {"requestBody": {"content": {"multipart/form-data": {"schema": schema}}}}

    def _upload_property_schema(self, source: str) -> dict[str, Any]:
        file_schema = {
            "type": "string",
            "format": "binary",
            "contentMediaType": "application/octet-stream",
        }
        if source == "files":
            return {"type": "array", "items": file_schema}
        return file_schema

    def _upload_max_items(
        self,
        controller: type,
        handler: Callable[..., Any],
        field_name: str,
        source: str,
    ) -> int | None:
        if source != "files":
            return None
        candidates = [
            *getattr(controller, "__fanest_interceptors__", []),
            *getattr(handler, "__fanest_interceptors__", []),
        ]
        for interceptor in candidates:
            instance = interceptor() if inspect.isclass(interceptor) else interceptor
            if isinstance(instance, FilesInterceptor) and instance.field_name == field_name:
                return instance.max_count
            if isinstance(instance, FileFieldsInterceptor):
                for upload_field in instance.upload_fields:
                    if upload_field.name == field_name:
                        return upload_field.max_count
            if isinstance(instance, AnyFilesInterceptor):
                return instance.max_count
            if isinstance(instance, FileInterceptor) and instance.field_name == field_name:
                return 1
        return None

    def _route_status_code(self, handler: Callable[..., Any]) -> int:
        route_metadata = getattr(handler, "__fanest_route__", None)
        if route_metadata is not None:
            code = route_metadata.options.get("status_code")
            if code is not None:
                return code
        pending = getattr(handler, "__fanest_pending_route_options__", None)
        if pending:
            code = pending.get("status_code")
            if code is not None:
                return code
        return 200

    def _response_headers(self, controller: Any, handler: Callable[..., Any]) -> list[tuple[str, str]]:
        controller_values = getattr(controller.__class__, "__fanest_response_headers__", [])
        handler_values = self._metadata(handler, "__fanest_response_headers__", [])
        return [*controller_values, *handler_values]

    _SINGULAR_RESPONSE_HEADERS = frozenset(
        {
            "content-type",
            "content-length",
            "content-disposition",
            "content-range",
            "location",
            "etag",
            "last-modified",
            "retry-after",
        }
    )

    def _append_response_headers(self, response: StarletteResponse, headers: list[tuple[str, str]]) -> None:
        for name, value in headers:
            # Singular headers (e.g. Content-Type) must override any existing value
            # rather than produce a duplicate header line, matching NestJS semantics.
            if name.lower() in self._SINGULAR_RESPONSE_HEADERS:
                response.headers[name] = value
            else:
                response.headers.append(name, value)

    def _with_response_headers(
        self,
        response: StarletteResponse,
        headers: list[tuple[str, str]],
    ) -> StarletteResponse:
        self._append_response_headers(response, headers)
        return response

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
