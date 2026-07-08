from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from fanest.core.container import FaNestContainer
from fanest.core.discovery import DiscoveryService
from fanest.core.enhancers import APP_ENHANCER_TOKENS, APP_FILTER, APP_GUARD, APP_INTERCEPTOR, APP_PIPE
from fanest.core.metadata import ClassProvider, ForwardRef, ValueProvider
from fanest.core.scanner import ModuleScanner
from fanest.common.middleware import FaNestMiddlewareAdapter
from fanest.platform_fastapi.adapter import FastApiAdapter
from fanest.schedule.registry import SchedulerRegistry
from fanest.schedule.runner import ScheduleRunner


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
        version: str = "0.1.0",
        description: str | None = None,
        debug: bool = False,
        overrides: dict[type, object] | None = None,
        global_prefix: str = "",
        cors: bool | dict[str, object] = False,
        global_guards: list[object] | None = None,
        global_pipes: list[object] | None = None,
        global_interceptors: list[object] | None = None,
        global_filters: list[object] | None = None,
    ) -> FastAPI:
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
            global_guards=global_guards,
            global_pipes=global_pipes,
            global_interceptors=global_interceptors,
            global_filters=global_filters,
        )

    @staticmethod
    async def create_async(
        root_module: Any,
        *,
        title: str = "FaNest Application",
        version: str = "0.1.0",
        description: str | None = None,
        debug: bool = False,
        overrides: dict[type, object] | None = None,
        global_prefix: str = "",
        cors: bool | dict[str, object] = False,
        global_guards: list[object] | None = None,
        global_pipes: list[object] | None = None,
        global_interceptors: list[object] | None = None,
        global_filters: list[object] | None = None,
    ) -> FastAPI:
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
            global_guards=global_guards,
            global_pipes=global_pipes,
            global_interceptors=global_interceptors,
            global_filters=global_filters,
        )

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
        global_guards: list[object] | None,
        global_pipes: list[object] | None,
        global_interceptors: list[object] | None,
        global_filters: list[object] | None,
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
                exports=set(record.metadata.exports),
                global_module=record.metadata.global_module,
            )
        container.register(
            ValueProvider(
                provide=DiscoveryService,
                use_value=DiscoveryService(container, scanner.providers, scanner.controllers),
            )
        )
        for token, value in (overrides or {}).items():
            container.override(token, value)
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
        for static_asset in scanner.static_assets:
            from fanest.platform_fastapi.modules import serve_static

            serve_static(app, static_asset["path"], static_asset["directory"], name=static_asset["name"])
        for middleware in scanner.app_middlewares:
            app.add_middleware(middleware["class"], **middleware["options"])
        if cors:
            options = cors if isinstance(cors, dict) else {"allow_origins": []}
            app.add_middleware(
                CORSMiddleware,
                allow_origins=cast(list[str], options.get("allow_origins", [])),
                allow_credentials=cast(bool, options.get("allow_credentials", False)),
                allow_methods=cast(list[str], options.get("allow_methods", ["GET"])),
                allow_headers=cast(list[str], options.get("allow_headers", [])),
            )
        for middleware in reversed(scanner.middlewares):
            app.add_middleware(
                FaNestMiddlewareAdapter,
                middleware=middleware,
                container=container,
            )
        adapter = FastApiAdapter(
            app=app,
            container=container,
            global_prefix=global_prefix,
            global_guards=resolved_global_guards,
            global_pipes=resolved_global_pipes,
            global_interceptors=resolved_global_interceptors,
            global_filters=resolved_global_filters,
            controller_modules=scanner.controller_modules,
            gateway_modules=scanner.gateway_modules,
        )
        adapter.register_controllers(scanner.controllers)
        adapter.register_gateways(scanner.gateways)
        FaNestFactory._register_validation_exception_handler(app, adapter)
        return app

    @staticmethod
    def _register_validation_exception_handler(app: FastAPI, adapter: FastApiAdapter) -> None:
        @app.exception_handler(RequestValidationError)
        async def fanest_validation_exception_handler(request: Any, exc: RequestValidationError):
            handled = await adapter.handle_validation_error(request, exc)
            if isinstance(handled, Response):
                return handled
            if handled is not None:
                return JSONResponse(status_code=400, content=handled)
            return JSONResponse(status_code=422, content={"detail": exc.errors()})

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
            global_guards[:] = [*await container.resolve_all_async(APP_GUARD), *explicit_global_guards]
            global_pipes[:] = [*await container.resolve_all_async(APP_PIPE), *explicit_global_pipes]
            global_interceptors[:] = [
                *await container.resolve_all_async(APP_INTERCEPTOR),
                *explicit_global_interceptors,
            ]
            global_filters[:] = [*await container.resolve_all_async(APP_FILTER), *explicit_global_filters]
            instances = []
            for module_key, record in records.items():
                for provider in [*record.metadata.providers, *record.metadata.gateways]:
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
                    if FaNestFactory._is_request_scoped_provider(container, provider, module_key=module_key):
                        continue
                    instance = await container.resolve_async(
                        container.provider_token(provider),
                        module_key=module_key,
                    )
                    instances.append(instance)
                    FaNestFactory._register_passport_strategy(container, instance, module_key=module_key)
                    hook = getattr(instance, "on_module_init", None)
                    if hook is not None:
                        await FaNestFactory._call_lifecycle_hook(hook)
            for instance in instances:
                hook = getattr(instance, "on_application_bootstrap", None)
                if hook is not None:
                    await FaNestFactory._call_lifecycle_hook(hook)
            schedule_runner = ScheduleRunner(instances, registry=container.resolve(SchedulerRegistry))
            schedule_runner.start()
            yield
            await schedule_runner.stop()
            for instance in reversed(instances):
                hook = getattr(instance, "before_application_shutdown", None)
                if hook is not None:
                    await FaNestFactory._call_lifecycle_hook(hook)
            for instance in reversed(instances):
                hook = getattr(instance, "on_module_destroy", None)
                if hook is not None:
                    await FaNestFactory._call_lifecycle_hook(hook)
            for instance in reversed(instances):
                hook = getattr(instance, "on_application_shutdown", None)
                if hook is not None:
                    await FaNestFactory._call_lifecycle_hook(hook)

        return lifespan

    @staticmethod
    async def _call_lifecycle_hook(hook):
        result = hook()
        if hasattr(result, "__await__"):
            await result

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
            event = getattr(handler, "__fanest_event__", None)
            if event is not None:
                emitter.on(
                    event,
                    FaNestFactory._lazy_dispatch_handler(
                        container,
                        provider,
                        handler.__name__,
                        module_key,
                    ),
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
        if not has_graphql_handlers:
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
    def _lazy_graphql_resolver(container: FaNestContainer, provider: type, module_key: Any | None):
        class LazyGraphQLResolver:
            pass

        resolver = LazyGraphQLResolver()
        for _, method in inspect.getmembers(provider, predicate=inspect.isfunction):
            metadata = getattr(method, "__fanest_graphql__", None)
            if metadata is None:
                continue

            def make_handler(method_name: str):
                async def handler(**kwargs: Any) -> Any:
                    return await FaNestFactory._call_lazy_provider_method(
                        container,
                        provider,
                        method_name,
                        module_key,
                        **kwargs,
                    )

                return handler

            handler = make_handler(method.__name__)
            setattr(
                handler,
                "__fanest_registration_key__",
                (module_key, provider, method.__name__, "graphql"),
            )
            setattr(handler, "__fanest_graphql__", metadata)
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
