from __future__ import annotations

import asyncio
import inspect
import os
import signal as signal_module
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from fanest.core.container import FaNestContainer
from fanest.core.discovery import DiscoveryService
from fanest._version import __version__ as _DEFAULT_FANEST_VERSION
from fanest.core.enhancers import APP_ENHANCER_TOKENS, APP_FILTER, APP_GUARD, APP_INTERCEPTOR, APP_PIPE
from fanest.core.metadata import ClassProvider, ForwardRef, ValueProvider
from fanest.core.scanner import ModuleScanner
from fanest.common.middleware import FaNestMiddlewareAdapter
from fanest.common.versioning import VersioningOptions, normalize_versioning_options
from fanest.platform_fastapi.adapter import FastApiAdapter
from fanest.schedule.registry import SchedulerRegistry
from fanest.schedule.runner import ScheduleRunner


class FaNestRawBodyMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        messages: list[dict[str, Any]] = []
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") == "http.request":
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            else:
                break
        scope["fanest.raw_body"] = bytes(body)
        iterator = iter(messages)

        async def replay_receive() -> dict[str, Any]:
            try:
                return next(iterator)
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


class FaNestFactory:
    @staticmethod
    def create_application(root_module: type, **options):
        from fanest.core.application import FaNestApplication

        return FaNestApplication(root_module, **options)

    @staticmethod
    def create(
        root_module: type,
        *,
        title: str = "FaNest Application",
        version: str | None = None,
        description: str | None = None,
        debug: bool = False,
        overrides: dict[type, object] | None = None,
        global_prefix: str = "",
        cors: bool | dict[str, object] = False,
        raw_body: bool = False,
        global_guards: list[object] | None = None,
        global_pipes: list[object] | None = None,
        global_interceptors: list[object] | None = None,
        global_filters: list[object] | None = None,
        versioning: VersioningOptions | dict[str, Any] | bool | None = None,
    ) -> FastAPI:
        if version is None:
            version = _DEFAULT_FANEST_VERSION
        scanner = ModuleScanner()
        scanner.scan(root_module)
        return FaNestFactory._create_from_scanner(
            scanner,
            root_module,
            title=title,
            version=version,
            description=description,
            debug=debug,
            overrides=overrides,
            global_prefix=global_prefix,
            cors=cors,
            raw_body=raw_body,
            global_guards=global_guards,
            global_pipes=global_pipes,
            global_interceptors=global_interceptors,
            global_filters=global_filters,
            versioning=versioning,
        )

    @staticmethod
    async def create_async(
        root_module: Any,
        *,
        title: str = "FaNest Application",
        version: str | None = None,
        description: str | None = None,
        debug: bool = False,
        overrides: dict[type, object] | None = None,
        global_prefix: str = "",
        cors: bool | dict[str, object] = False,
        raw_body: bool = False,
        global_guards: list[object] | None = None,
        global_pipes: list[object] | None = None,
        global_interceptors: list[object] | None = None,
        global_filters: list[object] | None = None,
        versioning: VersioningOptions | dict[str, Any] | bool | None = None,
    ) -> FastAPI:
        if version is None:
            version = _DEFAULT_FANEST_VERSION
        scanner = ModuleScanner()
        await scanner.scan_async(root_module)
        return FaNestFactory._create_from_scanner(
            scanner,
            root_module,
            title=title,
            version=version,
            description=description,
            debug=debug,
            overrides=overrides,
            global_prefix=global_prefix,
            cors=cors,
            raw_body=raw_body,
            global_guards=global_guards,
            global_pipes=global_pipes,
            global_interceptors=global_interceptors,
            global_filters=global_filters,
            versioning=versioning,
        )

    @staticmethod
    async def create_application_context(
        root_module: Any,
        *,
        overrides: dict[type, object] | None = None,
    ) -> "FaNestApplicationContext":
        """Create a non-HTTP FaNest application: a DI container plus the full
        provider lifecycle (``on_module_init`` / ``on_application_bootstrap``),
        with no web server. Ideal for CLI tools, cron workers, scripts and tests
        — the equivalent of NestJS ``NestFactory.createApplicationContext``.

            context = await FaNestFactory.create_application_context(AppModule)
            service = context.get(MyService)
            ...
            await context.close()

        The returned object is also an async context manager that closes itself.
        """
        scanner = ModuleScanner()
        await scanner.scan_async(root_module)
        container = FaNestFactory._build_context_container(scanner, root_module, overrides)
        context = FaNestApplicationContext(container, scanner.records, root_module)
        await context.init()
        return context

    @staticmethod
    def _build_context_container(
        scanner: ModuleScanner,
        root_module: Any,
        overrides: dict[type, object] | None,
    ) -> FaNestContainer:
        container = FaNestContainer()
        if scanner.records:
            container.set_root_module(next(iter(scanner.records)))
        for module_key, record in scanner.records.items():
            imports = [scanner._module_key(imported_module) for imported_module in record.metadata.imports]
            container.register_module(
                module_key,
                providers=[
                    *record.metadata.providers,
                    *record.metadata.gateways,
                    *record.metadata.controllers,
                ],
                imports=imports,
                exports=scanner.export_tokens(module_key),
                global_module=record.metadata.global_module,
            )
        container.register(
            ValueProvider(
                provide=DiscoveryService,
                use_value=DiscoveryService(container, scanner.providers, scanner.controllers, scanner.records),
            )
        )
        for token, value in (overrides or {}).items():
            container.override(token, value)
        FaNestFactory._register_cqrs_handler_providers(scanner.records, container)
        return container

    @staticmethod
    def _create_from_scanner(
        scanner: ModuleScanner,
        root_module: Any,
        *,
        title: str,
        version: str,
        description: str | None,
        debug: bool,
        overrides: dict[type, object] | None,
        global_prefix: str,
        cors: bool | dict[str, object],
        raw_body: bool,
        global_guards: list[object] | None,
        global_pipes: list[object] | None,
        global_interceptors: list[object] | None,
        global_filters: list[object] | None,
        versioning: VersioningOptions | dict[str, Any] | bool | None,
    ) -> FastAPI:

        container = FaNestContainer()
        if scanner.records:
            container.set_root_module(next(iter(scanner.records)))
        module_import_keys: dict[Any, list[Any]] = {}
        for module_key, record in scanner.records.items():
            imports = [scanner._module_key(imported_module) for imported_module in record.metadata.imports]
            module_import_keys[module_key] = imports
            container.register_module(
                module_key,
                providers=[
                    *record.metadata.providers,
                    *record.metadata.gateways,
                    *record.metadata.controllers,
                ],
                imports=imports,
                exports=scanner.export_tokens(module_key),
                global_module=record.metadata.global_module,
            )
        container.register(
            ValueProvider(
                provide=DiscoveryService,
                use_value=DiscoveryService(container, scanner.providers, scanner.controllers, scanner.records),
            )
        )
        for token, value in (overrides or {}).items():
            container.override(token, value)
        FaNestFactory._register_cqrs_handler_providers(scanner.records, container)
        for component in [
            *(global_guards or []),
            *(global_pipes or []),
            *(global_interceptors or []),
            *(global_filters or []),
        ]:
            if inspect.isclass(component) and not container.has_provider(component):
                container.register(component)

        resolved_global_guards = [*container.resolve_all_ready(APP_GUARD), *(global_guards or [])]
        resolved_global_pipes = [*container.resolve_all_ready(APP_PIPE), *(global_pipes or [])]
        resolved_global_interceptors = [
            *container.resolve_all_ready(APP_INTERCEPTOR),
            *(global_interceptors or []),
        ]
        resolved_global_filters = [*container.resolve_all_ready(APP_FILTER), *(global_filters or [])]
        lifespan = FaNestFactory._lifespan(
            scanner.records,
            container,
            global_guards=resolved_global_guards,
            explicit_global_guards=list(global_guards or []),
            global_pipes=resolved_global_pipes,
            explicit_global_pipes=list(global_pipes or []),
            global_interceptors=resolved_global_interceptors,
            explicit_global_interceptors=list(global_interceptors or []),
            global_filters=resolved_global_filters,
            explicit_global_filters=list(global_filters or []),
        )
        app_options: dict[str, Any] = {
            "title": title,
            "version": version,
            "debug": debug,
            "lifespan": lifespan,
        }
        if description is not None:
            app_options["description"] = description
        app = FastAPI(**app_options)
        app.state.fanest_container = container
        app.state.fanest_root_module = root_module
        app.state.fanest_microservices = []
        FaNestFactory._attach_microservice_lifecycle(app, root_module)
        for static_asset in scanner.static_assets:
            from fanest.platform_fastapi.modules import serve_static

            serve_static(
                app,
                static_asset["path"],
                static_asset["directory"],
                name=static_asset["name"],
                html=cast(bool, static_asset.get("html", False)),
                check_dir=cast(bool, static_asset.get("check_dir", True)),
                follow_symlink=cast(bool, static_asset.get("follow_symlink", False)),
                packages=static_asset.get("packages"),
            )
        for middleware in scanner.app_middlewares:
            app.add_middleware(middleware["class"], **middleware["options"])
        if cors:
            options = FaNestFactory._cors_options(cors)
            app.add_middleware(
                CORSMiddleware,
                allow_origins=cast(list[str], options.get("allow_origins", [])),
                allow_credentials=cast(bool, options.get("allow_credentials", False)),
                allow_methods=cast(list[str], options.get("allow_methods", ["GET"])),
                allow_headers=cast(list[str], options.get("allow_headers", [])),
                allow_origin_regex=cast(str | None, options.get("allow_origin_regex")),
                expose_headers=cast(list[str], options.get("expose_headers", [])),
                max_age=cast(int, options.get("max_age", 600)),
            )
        if raw_body:
            app.add_middleware(FaNestRawBodyMiddleware)
        for middleware in reversed(scanner.middlewares):
            app.add_middleware(
                FaNestMiddlewareAdapter,
                middleware=middleware,
                container=container,
            )
        module_route_prefixes = {
            module_key: scanner.router_paths[record.module_type]
            for module_key, record in scanner.records.items()
            if record.module_type in scanner.router_paths
        }
        adapter = FastApiAdapter(
            app=app,
            container=container,
            global_prefix=FaNestFactory._global_prefix(global_prefix),
            global_guards=resolved_global_guards,
            global_pipes=resolved_global_pipes,
            global_interceptors=resolved_global_interceptors,
            global_filters=resolved_global_filters,
            versioning=normalize_versioning_options(versioning),
            controller_modules=scanner.controller_modules,
            gateway_modules=scanner.gateway_modules,
            module_route_prefixes=module_route_prefixes,
        )
        app.state.fanest_http_adapter = adapter
        adapter.register_controllers(scanner.controllers)
        adapter.register_gateways(scanner.gateways)
        FaNestFactory._register_validation_exception_handler(app, adapter)
        return app

    @staticmethod
    def _attach_microservice_lifecycle(app: FastAPI, root_module: Any) -> None:
        from fanest.microservices import MicroserviceServer, Transport

        def connect_microservice(options: dict[str, Any] | None = None, **kwargs: Any) -> MicroserviceServer:
            merged = {**(options or {}), **kwargs}
            transport = merged.pop("transport", Transport.MEMORY)
            module = merged.pop("module", root_module)
            server = MicroserviceServer.create(module, transport=transport, **merged).compile()
            app.state.fanest_microservices.append(server)
            return server

        async def start_all_microservices() -> list[MicroserviceServer]:
            services = list(app.state.fanest_microservices)
            for server in services:
                await server.listen()
            return services

        async def close_all_microservices() -> None:
            services = list(app.state.fanest_microservices)
            for server in reversed(services):
                await server.close()

        setattr(app, "connect_microservice", connect_microservice)
        setattr(app, "start_all_microservices", start_all_microservices)
        setattr(app, "close_all_microservices", close_all_microservices)

    @staticmethod
    def _cors_options(cors: bool | dict[str, object]) -> dict[str, object]:
        options: dict[str, object] = dict(cors) if isinstance(cors, dict) else {"allow_origins": []}
        for key in ("allow_origins", "allow_methods", "allow_headers", "expose_headers"):
            if key in options:
                options[key] = FaNestFactory._cors_string_list(key, options[key])
        if "allow_origin_regex" in options and not isinstance(options["allow_origin_regex"], str | type(None)):
            raise ValueError("CORS allow_origin_regex must be a string")
        if "max_age" in options:
            max_age = options["max_age"]
            if not isinstance(max_age, int) or max_age < 0:
                raise ValueError("CORS max_age must be a non-negative integer")
        allow_origins = cast(list[str], options.get("allow_origins", []))
        allow_credentials = cast(bool, options.get("allow_credentials", False))
        if not isinstance(allow_credentials, bool):
            raise ValueError("CORS allow_credentials must be a boolean")
        if allow_credentials and "*" in allow_origins:
            raise ValueError("CORS allow_credentials=True cannot be used with wildcard allow_origins")
        return options

    @staticmethod
    def _cors_string_list(key: str, value: object) -> list[str]:
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list | tuple | set):
            values = list(value)
        else:
            raise ValueError(f"CORS {key} must be a string or a list of strings")
        if any(not isinstance(item, str) for item in values):
            raise ValueError(f"CORS {key} must contain only strings")
        normalized = [str(item).strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError(f"CORS {key} cannot contain empty values")
        return normalized

    @staticmethod
    def _global_prefix(prefix: str) -> str:
        if not isinstance(prefix, str):
            raise ValueError("global_prefix must be a string")
        normalized = prefix.strip("/")
        if normalized in {"", "."}:
            return ""
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("global_prefix cannot contain empty, dot, or parent directory segments")
        return normalized

    @staticmethod
    def _register_validation_exception_handler(app: FastAPI, adapter: FastApiAdapter) -> None:
        @app.exception_handler(RequestValidationError)
        async def fanest_validation_exception_handler(request: Any, exc: RequestValidationError):
            handled = await adapter.handle_validation_error(request, exc)
            if isinstance(handled, Response):
                return handled
            if handled is not None:
                return JSONResponse(status_code=400, content=handled)
            return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    @staticmethod
    def _lifespan(
        records: dict[Any, Any],
        container: FaNestContainer,
        *,
        global_guards: list[Any],
        explicit_global_guards: list[Any],
        global_pipes: list[Any],
        explicit_global_pipes: list[Any],
        global_interceptors: list[Any],
        explicit_global_interceptors: list[Any],
        global_filters: list[Any],
        explicit_global_filters: list[Any],
    ):
        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            resolved_guards = await container.resolve_all_async(APP_GUARD)
            resolved_pipes = await container.resolve_all_async(APP_PIPE)
            resolved_interceptors = await container.resolve_all_async(APP_INTERCEPTOR)
            resolved_filters = await container.resolve_all_async(APP_FILTER)
            global_guards[:] = [*resolved_guards, *explicit_global_guards]
            global_pipes[:] = [*resolved_pipes, *explicit_global_pipes]
            global_interceptors[:] = [*resolved_interceptors, *explicit_global_interceptors]
            global_filters[:] = [*resolved_filters, *explicit_global_filters]
            instances, schedule_runner = await FaNestFactory._bootstrap_instances(
                records,
                container,
                extra_instances=[
                    *resolved_guards,
                    *resolved_pipes,
                    *resolved_interceptors,
                    *resolved_filters,
                ],
            )
            shutdown_state: dict[str, Any] = {"signal": None, "done": False}

            async def shutdown_once() -> None:
                if shutdown_state["done"]:
                    return
                shutdown_state["done"] = True
                close_all_microservices = getattr(app, "close_all_microservices", None)
                if close_all_microservices is not None:
                    await close_all_microservices()
                await FaNestFactory._shutdown_instances(
                    instances,
                    schedule_runner,
                    signal_name=shutdown_state["signal"],
                )

            previous_handlers = FaNestFactory._install_shutdown_signal_handlers(
                getattr(app.state, "fanest_shutdown_hooks", None),
                shutdown_state,
                shutdown_once,
            )
            try:
                yield
            finally:
                FaNestFactory._restore_signal_handlers(previous_handlers)
                await shutdown_once()

        return lifespan

    @staticmethod
    def _install_shutdown_signal_handlers(
        shutdown_signals: Any,
        shutdown_state: dict[str, Any],
        shutdown_once: Any,
    ) -> dict[Any, Any]:
        """Chain graceful-shutdown handlers onto the requested signals.

        When the previous handler is callable (e.g. uvicorn's), it is invoked
        after recording the signal so the server drives lifespan shutdown and
        the hooks receive the signal name. When there is no meaningful previous
        handler, the hooks run on the event loop and the default disposition is
        re-raised afterwards, mirroring Nest's ``enableShutdownHooks``.
        """
        if not shutdown_signals:
            return {}
        loop = asyncio.get_running_loop()
        requested = (
            shutdown_signals
            if isinstance(shutdown_signals, (list, tuple, set))
            else ("SIGTERM", "SIGINT")
        )
        previous_handlers: dict[Any, Any] = {}

        def _handler(received_signum: int, frame: Any) -> None:
            shutdown_state["signal"] = signal_module.Signals(received_signum).name
            previous = previous_handlers.get(received_signum)
            if callable(previous):
                previous(received_signum, frame)
                return

            async def _shutdown_and_exit() -> None:
                await shutdown_once()
                signal_module.signal(received_signum, signal_module.SIG_DFL)
                os.kill(os.getpid(), received_signum)

            asyncio.run_coroutine_threadsafe(_shutdown_and_exit(), loop)

        for requested_signal in requested:
            signum = (
                signal_module.Signals[requested_signal]
                if isinstance(requested_signal, str)
                else signal_module.Signals(requested_signal)
            )
            try:
                previous = signal_module.getsignal(signum)
                signal_module.signal(signum, _handler)
            except (ValueError, OSError):
                # Not on the main thread (e.g. TestClient portals) or an
                # uncatchable signal: shutdown hooks still run via lifespan.
                continue
            previous_handlers[signum] = previous
        return previous_handlers

    @staticmethod
    def _restore_signal_handlers(previous_handlers: dict[Any, Any]) -> None:
        for signum, previous in previous_handlers.items():
            try:
                signal_module.signal(signum, previous)
            except (ValueError, OSError, TypeError):
                continue

    @staticmethod
    async def _bootstrap_instances(
        records: dict[Any, Any],
        container: FaNestContainer,
        *,
        extra_instances: list[Any] | None = None,
    ):
        """Instantiate every non-request-scoped provider, register discovered
        event/graphql/cqrs/queue/worker providers, run ``on_module_init`` then
        ``on_application_bootstrap`` hooks, and start the schedule runner.

        ``extra_instances`` are already-resolved instances (e.g. global
        APP_* enhancer instances) that participate in the lifecycle so their
        ``on_module_init`` / ``on_application_bootstrap`` / shutdown hooks fire
        exactly once, like any other DI provider.

        Shared by the HTTP lifespan and the standalone application context.
        """
        instances: list[Any] = []
        seen_instance_ids: set[int] = set()
        ordered_records = FaNestFactory._lifecycle_records(records)
        for module_key, record in ordered_records:
            # Controllers participate in the lifecycle exactly like providers in
            # NestJS: they are eagerly instantiated at bootstrap and their
            # on_module_init / on_application_bootstrap / shutdown hooks fire.
            for provider in [
                *record.metadata.providers,
                *record.metadata.gateways,
                *record.metadata.controllers,
            ]:
                if container.provider_token(provider) in APP_ENHANCER_TOKENS:
                    continue
                provider_type = FaNestFactory._provider_type(provider)
                if provider_type is not None:
                    FaNestFactory._register_event_provider(container, provider_type, module_key=module_key)
                    FaNestFactory._register_graphql_resolver_provider(
                        container,
                        provider_type,
                        module_key=module_key,
                    )
                    FaNestFactory._register_cqrs_handler_provider(
                        container,
                        provider_type,
                        module_key=module_key,
                    )
                    FaNestFactory._register_queue_processor_provider(
                        container,
                        provider_type,
                        module_key=module_key,
                    )
                    FaNestFactory._register_worker_task_provider(
                        container,
                        provider_type,
                        module_key=module_key,
                    )
                if FaNestFactory._is_non_singleton_provider(container, provider, module_key=module_key):
                    continue
                instance = await container.resolve_async(
                    container.provider_token(provider),
                    module_key=module_key,
                )
                instance_id = id(instance)
                if instance_id in seen_instance_ids:
                    continue
                seen_instance_ids.add(instance_id)
                instances.append(instance)
                FaNestFactory._register_passport_strategy(container, instance, module_key=module_key)
                hook = getattr(instance, "on_module_init", None)
                if hook is not None:
                    await FaNestFactory._call_lifecycle_hook(hook)
        for instance in extra_instances or []:
            # Request/transient-scoped global enhancers are passed as their class
            # (resolved fresh per request), not an instance — they have no
            # singleton lifecycle to run here.
            if inspect.isclass(instance):
                continue
            instance_id = id(instance)
            if instance_id in seen_instance_ids:
                continue
            seen_instance_ids.add(instance_id)
            instances.append(instance)
            hook = getattr(instance, "on_module_init", None)
            if hook is not None:
                await FaNestFactory._call_lifecycle_hook(hook)
        for instance in instances:
            hook = getattr(instance, "on_application_bootstrap", None)
            if hook is not None:
                await FaNestFactory._call_lifecycle_hook(hook)
        schedule_runner = ScheduleRunner(instances, registry=container.resolve(SchedulerRegistry))
        schedule_runner.start()
        return instances, schedule_runner

    @staticmethod
    async def _shutdown_instances(
        instances: list[Any],
        schedule_runner: Any,
        signal_name: str | None = None,
    ) -> None:
        """Stop the schedule runner and run ``on_module_destroy``,
        ``before_application_shutdown`` and ``on_application_shutdown`` hooks in
        reverse registration order — matching Nest's documented shutdown
        sequence (``onModuleDestroy`` → ``beforeApplicationShutdown`` →
        ``onApplicationShutdown``). Shared by the HTTP lifespan and the context.
        ``before_application_shutdown`` and ``on_application_shutdown`` hooks
        that accept a positional parameter receive the triggering signal name
        (or ``None``), matching Nest."""
        await schedule_runner.stop()
        for instance in reversed(instances):
            hook = getattr(instance, "on_module_destroy", None)
            if hook is not None:
                await FaNestFactory._call_lifecycle_hook(hook)
        for instance in reversed(instances):
            hook = getattr(instance, "before_application_shutdown", None)
            if hook is not None:
                await FaNestFactory._call_lifecycle_hook(hook, signal_name)
        for instance in reversed(instances):
            hook = getattr(instance, "on_application_shutdown", None)
            if hook is not None:
                await FaNestFactory._call_lifecycle_hook(hook, signal_name)

    @staticmethod
    async def _call_lifecycle_hook(hook, *args):
        if args and not FaNestFactory._hook_accepts_arguments(hook, len(args)):
            args = ()
        result = hook(*args)
        if hasattr(result, "__await__"):
            await result

    @staticmethod
    def _hook_accepts_arguments(hook: Any, count: int) -> bool:
        try:
            parameters = inspect.signature(hook).parameters.values()
        except (TypeError, ValueError):
            return False
        positional = 0
        for parameter in parameters:
            if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                return True
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                positional += 1
        return positional >= count

    @staticmethod
    def _lifecycle_records(records: dict[Any, Any]) -> list[tuple[Any, Any]]:
        ordered: list[tuple[Any, Any]] = []
        seen: set[Any] = set()

        def visit(module_key: Any) -> None:
            if module_key in seen:
                return
            seen.add(module_key)
            record = records[module_key]
            for imported_module in record.metadata.imports:
                imported_key = FaNestFactory._record_import_key(records, imported_module)
                if imported_key in records:
                    visit(imported_key)
            ordered.append((module_key, record))

        for module_key in records:
            visit(module_key)
        return ordered

    @staticmethod
    def _record_import_key(records: dict[Any, Any], imported_module: Any) -> Any:
        for module_key, record in records.items():
            if record.module is imported_module:
                return module_key
        for module_key, record in records.items():
            if record.module_type is imported_module:
                return module_key
        return imported_module

    @staticmethod
    def _provider_type(provider: Any) -> type | None:
        if isinstance(provider, ForwardRef):
            return FaNestFactory._provider_type(provider.factory())
        if isinstance(provider, ClassProvider):
            return provider.use_class
        if inspect.isclass(provider):
            return provider
        return None

    @staticmethod
    def _is_request_scoped_provider(
        container: FaNestContainer,
        provider: Any,
        *,
        module_key: Any | None = None,
    ) -> bool:
        token = container.provider_token(provider)
        _, located_provider = container._locate_provider(token, module_key)
        if located_provider is None:
            return False
        return container._effective_scope(token, located_provider, module_key=module_key) == "request"

    @staticmethod
    def _is_non_singleton_provider(
        container: FaNestContainer,
        provider: Any,
        *,
        module_key: Any | None = None,
    ) -> bool:
        token = container.provider_token(provider)
        _, located_provider = container._locate_provider(token, module_key)
        if located_provider is None:
            return False
        return container._effective_scope(token, located_provider, module_key=module_key) != "singleton"

    @staticmethod
    def _register_event_provider(
        container: FaNestContainer,
        provider: type,
        *,
        module_key: Any | None = None,
    ) -> None:
        from fanest.events import EventEmitter

        try:
            emitter = container.resolve(EventEmitter, module_key=module_key)
        except Exception:
            return
        for _, handler in inspect.getmembers(provider, predicate=inspect.isfunction):
            subscriptions = getattr(handler, "__fanest_events__", None)
            if not subscriptions:
                event = getattr(handler, "__fanest_event__", None)
                if event is None:
                    continue
                subscriptions = [
                    {
                        "event": event,
                        "priority": getattr(handler, "__fanest_event_priority__", 0),
                        "prepend": getattr(handler, "__fanest_event_prepend__", False),
                    }
                ]
            for subscription in subscriptions:
                emitter.on(
                    subscription["event"],
                    FaNestFactory._lazy_dispatch_handler(
                        container,
                        provider,
                        handler.__name__,
                        module_key,
                    ),
                    prepend=subscription.get("prepend", False),
                    priority=subscription.get("priority", 0),
                )

    @staticmethod
    def _register_queue_processors(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        FaNestFactory._register_queue_processor_provider(container, instance.__class__, module_key=module_key)

    @staticmethod
    def _register_queue_processor_provider(
        container: FaNestContainer,
        provider: type,
        *,
        module_key: Any | None = None,
    ) -> None:
        from fanest.queues import QueueService

        queue = getattr(provider, "__fanest_queue__", None)
        if queue is None:
            return
        try:
            queue_service = container.resolve(QueueService, module_key=module_key)
        except Exception:
            return
        for _, handler in inspect.getmembers(provider, predicate=inspect.isfunction):
            job_name = getattr(handler, "__fanest_process__", None)
            if job_name is not None:
                queue_service.register_processor(
                    queue,
                    job_name,
                    FaNestFactory._lazy_job_handler(
                        container,
                        provider,
                        handler.__name__,
                        module_key,
                    ),
                )

    @staticmethod
    def _register_graphql_resolver(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        FaNestFactory._register_graphql_resolver_provider(container, instance.__class__, module_key=module_key)

    @staticmethod
    def _register_graphql_resolver_provider(
        container: FaNestContainer,
        provider: type,
        *,
        module_key: Any | None = None,
    ) -> None:
        from fanest.graphql import GraphQLSchema

        if getattr(provider, "__fanest_provider__", None) is None:
            return
        has_graphql_handlers = any(
            getattr(handler, "__fanest_graphql__", None) is not None
            for _, handler in inspect.getmembers(provider, predicate=inspect.isfunction)
        )
        has_graphql_type_metadata = getattr(provider, "__fanest_graphql_type__", None) is not None
        if not has_graphql_handlers and not has_graphql_type_metadata:
            return
        try:
            schema = container.resolve(GraphQLSchema, module_key=module_key)
        except Exception:
            return
        schema.register_resolver(
            FaNestFactory._lazy_graphql_resolver(container, provider, module_key)
        )

    @staticmethod
    def _register_cqrs_handlers(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        FaNestFactory._register_cqrs_handler_provider(container, instance.__class__, module_key=module_key)

    @staticmethod
    def _register_cqrs_handler_providers(records: dict[Any, Any], container: FaNestContainer) -> None:
        for module_key, record in records.items():
            for provider in [*record.metadata.providers, *record.metadata.gateways]:
                provider_type = FaNestFactory._provider_type(provider)
                if provider_type is None:
                    continue
                FaNestFactory._register_cqrs_handler_provider(
                    container,
                    provider_type,
                    module_key=module_key,
                )

    @staticmethod
    def _register_cqrs_handler_provider(
        container: FaNestContainer,
        provider: type,
        *,
        module_key: Any | None = None,
    ) -> None:
        from fanest.cqrs import CommandBus, EventBus, QueryBus

        command = getattr(provider, "__fanest_command_handler__", None)
        if command is not None:
            try:
                container.resolve(CommandBus, module_key=module_key).register(
                    command,
                    FaNestFactory._lazy_cqrs_handler(container, provider, module_key),
                )
            except Exception:
                pass
        query = getattr(provider, "__fanest_query_handler__", None)
        if query is not None:
            try:
                container.resolve(QueryBus, module_key=module_key).register(
                    query,
                    FaNestFactory._lazy_cqrs_handler(container, provider, module_key),
                )
            except Exception:
                pass
        for event in getattr(provider, "__fanest_event_handlers__", []):
            try:
                container.resolve(EventBus, module_key=module_key).register(
                    event,
                    FaNestFactory._lazy_cqrs_handler(container, provider, module_key),
                )
            except Exception:
                pass
        for method_name, method in inspect.getmembers(provider, predicate=inspect.isfunction):
            event = getattr(method, "__fanest_cqrs_saga__", None)
            if event is None:
                continue
            try:
                container.resolve(EventBus, module_key=module_key).register_saga(
                    event,
                    FaNestFactory._lazy_cqrs_saga(container, provider, method_name, module_key),
                )
            except Exception:
                pass

    @staticmethod
    async def _call_lazy_provider_method(
        container: FaNestContainer,
        provider: type,
        method_name: str,
        module_key: Any | None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        owns_scope = container.current_request_instances() is None
        request_scope = container.begin_request() if owns_scope else None
        try:
            instance = await container.resolve_async(provider, module_key=module_key)
            result = getattr(instance, method_name)(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            if owns_scope and request_scope is not None:
                container.end_request(request_scope)

    @staticmethod
    def _lazy_dispatch_handler(
        container: FaNestContainer,
        provider: type,
        method_name: str,
        module_key: Any | None,
    ):
        async def handler(payload: Any = None) -> Any:
            return await FaNestFactory._call_lazy_provider_method(
                container,
                provider,
                method_name,
                module_key,
                payload,
            )

        setattr(handler, "__fanest_registration_key__", (module_key, provider, method_name, "event"))
        return handler

    @staticmethod
    def _lazy_cqrs_handler(container: FaNestContainer, provider: type, module_key: Any | None):
        class LazyCqrsHandler:
            async def execute(self, message: Any) -> Any:
                return await FaNestFactory._call_lazy_provider_method(
                    container,
                    provider,
                    "execute",
                    module_key,
                    message,
                )

            async def handle(self, message: Any) -> Any:
                return await FaNestFactory._call_lazy_provider_method(
                    container,
                    provider,
                    "handle",
                    module_key,
                    message,
                )

        handler = LazyCqrsHandler()
        setattr(handler, "__fanest_registration_key__", (module_key, provider, "cqrs"))
        return handler

    @staticmethod
    def _lazy_cqrs_saga(
        container: FaNestContainer,
        provider: type,
        method_name: str,
        module_key: Any | None,
    ):
        async def saga(event: Any) -> Any:
            return await FaNestFactory._call_lazy_provider_method(
                container,
                provider,
                method_name,
                module_key,
                event,
            )

        setattr(saga, "__fanest_registration_key__", (module_key, provider, method_name, "cqrs_saga"))
        return saga

    @staticmethod
    def _lazy_graphql_resolver(container: FaNestContainer, provider: type, module_key: Any | None):
        class LazyGraphQLResolver:
            pass

        resolver = LazyGraphQLResolver()
        type_metadata = getattr(provider, "__fanest_graphql_type__", None)
        if type_metadata is not None:
            setattr(resolver.__class__, "__fanest_graphql_type__", type_metadata)
        for _, method in inspect.getmembers(provider, predicate=inspect.isfunction):
            metadata = getattr(method, "__fanest_graphql__", None)
            field_metadata = getattr(method, "__fanest_graphql_field__", None)
            is_reference_resolver = (
                method.__name__ in {"resolve_reference", "__resolve_reference__"}
                or getattr(method, "__fanest_graphql_resolve_reference__", False)
            )
            if metadata is None and field_metadata is None and not is_reference_resolver:
                continue

            def make_handler(method_name: str):
                async def handler(*args: Any, **kwargs: Any) -> Any:
                    return await FaNestFactory._call_lazy_provider_method(
                        container,
                        provider,
                        method_name,
                        module_key,
                        *args,
                        **kwargs,
                    )

                return handler

            handler = make_handler(method.__name__)
            setattr(
                handler,
                "__fanest_registration_key__",
                (module_key, provider, method.__name__, "graphql"),
            )
            if metadata is not None:
                setattr(handler, "__fanest_graphql__", metadata)
            if field_metadata is not None:
                setattr(handler, "__fanest_graphql_field__", field_metadata)
            if is_reference_resolver:
                setattr(handler, "__fanest_graphql_resolve_reference__", True)
            for key in ("__fanest_guards__", "__fanest_pipes__", "__fanest_interceptors__"):
                values = getattr(method, key, None)
                if values is not None:
                    setattr(handler, key, list(values))
            setattr(handler, "__fanest_target_signature__", inspect.signature(method))
            setattr(resolver, method.__name__, handler)
        return resolver

    @staticmethod
    def _lazy_job_handler(
        container: FaNestContainer,
        provider: type,
        method_name: str,
        module_key: Any | None,
    ):
        async def handler(job: Any) -> Any:
            return await FaNestFactory._call_lazy_provider_method(
                container,
                provider,
                method_name,
                module_key,
                job,
            )

        setattr(handler, "__fanest_registration_key__", (module_key, provider, method_name, "queue"))
        return handler

    @staticmethod
    def _lazy_task_handler(
        container: FaNestContainer,
        provider: type,
        method_name: str,
        module_key: Any | None,
    ):
        async def handler(payload: Any = None) -> Any:
            return await FaNestFactory._call_lazy_provider_method(
                container,
                provider,
                method_name,
                module_key,
                payload,
            )

        setattr(handler, "__fanest_registration_key__", (module_key, provider, method_name, "worker"))
        return handler

    @staticmethod
    def _register_passport_strategy(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        from fanest.auth.passport import PassportService, PassportStrategy

        if not isinstance(instance, PassportStrategy):
            return
        try:
            container.resolve(PassportService, module_key=module_key).register(instance)
        except Exception:
            pass

    @staticmethod
    def _register_worker_tasks(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        FaNestFactory._register_worker_task_provider(container, instance.__class__, module_key=module_key)

    @staticmethod
    def _register_worker_task_provider(
        container: FaNestContainer,
        provider: type,
        *,
        module_key: Any | None = None,
    ) -> None:
        from fanest.workers import WorkerService

        try:
            workers = container.resolve(WorkerService, module_key=module_key)
        except Exception:
            return
        for _, handler in inspect.getmembers(provider, predicate=inspect.isfunction):
            task_name = getattr(handler, "__fanest_task_handler__", None)
            if task_name is not None:
                workers.register(
                    task_name,
                    FaNestFactory._lazy_task_handler(
                        container,
                        provider,
                        handler.__name__,
                        module_key,
                    ),
                )


class FaNestApplicationContext:
    """A non-HTTP FaNest application: a DI container plus the provider lifecycle,
    without a web server. The Python equivalent of NestJS
    ``NestFactory.createApplicationContext``. Create it via
    ``await FaNestFactory.create_application_context(AppModule)``.

    Resolve providers with :meth:`get` (sync) or :meth:`resolve` (async), and
    release resources with :meth:`close`. It is also an async context manager::

        async with await FaNestFactory.create_application_context(AppModule) as ctx:
            await ctx.get(ReportService).run()
    """

    def __init__(self, container: "FaNestContainer", records: dict[Any, Any], root_module: Any) -> None:
        self._container = container
        self._records = records
        self._root_module = root_module
        self._instances: list[Any] = []
        self._schedule_runner: Any = None
        self._started = False
        self._closed = False

    @property
    def container(self) -> "FaNestContainer":
        return self._container

    def get(self, token: Any, *, module_key: Any | None = None) -> Any:
        """Resolve a provider synchronously (like NestJS ``app.get(Token)``)."""
        return self._container.resolve(token, module_key=module_key)

    async def resolve(self, token: Any, *, module_key: Any | None = None) -> Any:
        """Resolve a provider, awaiting async factories/dependencies."""
        return await self._container.resolve_async(token, module_key=module_key)

    async def init(self) -> "FaNestApplicationContext":
        """Instantiate providers and run bootstrap lifecycle hooks (idempotent)."""
        if self._started:
            return self
        self._started = True
        self._instances, self._schedule_runner = await FaNestFactory._bootstrap_instances(
            self._records, self._container
        )
        return self

    async def close(self) -> None:
        """Run shutdown lifecycle hooks and stop scheduled jobs (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if self._started and self._schedule_runner is not None:
            await FaNestFactory._shutdown_instances(self._instances, self._schedule_runner)

    async def __aenter__(self) -> "FaNestApplicationContext":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()
