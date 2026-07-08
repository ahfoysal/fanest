from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fanest.core.container import FaNestContainer
from fanest.core.discovery import DiscoveryService
from fanest.core.enhancers import APP_ENHANCER_TOKENS, APP_FILTER, APP_GUARD, APP_INTERCEPTOR, APP_PIPE
from fanest.core.metadata import ValueProvider
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

        container = FaNestContainer()
        module_import_keys: dict[Any, list[Any]] = {}
        for module_key, record in scanner.records.items():
            imports = [scanner._module_key(imported_module) for imported_module in record.metadata.imports]
            module_import_keys[module_key] = imports
            container.register_module(
                module_key,
                providers=[*record.metadata.providers, *record.metadata.gateways],
                imports=imports,
                exports=set(record.metadata.exports),
                global_module=record.metadata.global_module,
            )
        for provider in scanner.providers:
            container.register(provider)
        container.register(
            ValueProvider(
                provide=DiscoveryService,
                use_value=DiscoveryService(container, scanner.providers, scanner.controllers),
            )
        )
        for token, value in (overrides or {}).items():
            container.override(token, value)

        lifespan = FaNestFactory._lifespan(scanner.records, container)
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
            options = cors if isinstance(cors, dict) else {}
            app.add_middleware(
                CORSMiddleware,
                allow_origins=cast(list[str], options.get("allow_origins", ["*"])),
                allow_credentials=cast(bool, options.get("allow_credentials", False)),
                allow_methods=cast(list[str], options.get("allow_methods", ["*"])),
                allow_headers=cast(list[str], options.get("allow_headers", ["*"])),
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
            global_guards=[*container.resolve_all(APP_GUARD), *(global_guards or [])],
            global_pipes=[*container.resolve_all(APP_PIPE), *(global_pipes or [])],
            global_interceptors=[*container.resolve_all(APP_INTERCEPTOR), *(global_interceptors or [])],
            global_filters=[*container.resolve_all(APP_FILTER), *(global_filters or [])],
            controller_modules=scanner.controller_modules,
            gateway_modules=scanner.gateway_modules,
        )
        adapter.register_controllers(scanner.controllers)
        adapter.register_gateways(scanner.gateways)
        return app

    @staticmethod
    def _lifespan(records: dict[Any, Any], container: FaNestContainer):
        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            instances = []
            for module_key, record in records.items():
                for provider in [*record.metadata.providers, *record.metadata.gateways]:
                    if container.provider_token(provider) in APP_ENHANCER_TOKENS:
                        continue
                    instance = await container.resolve_async(
                        container.provider_token(provider),
                        module_key=module_key,
                    )
                    instances.append(instance)
                    FaNestFactory._register_events(container, instance, module_key=module_key)
                    FaNestFactory._register_queue_processors(container, instance, module_key=module_key)
                    FaNestFactory._register_graphql_resolver(container, instance, module_key=module_key)
                    FaNestFactory._register_cqrs_handlers(container, instance, module_key=module_key)
                    FaNestFactory._register_passport_strategy(container, instance, module_key=module_key)
                    FaNestFactory._register_worker_tasks(container, instance, module_key=module_key)
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
    def _register_events(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        from fanest.events import EventEmitter

        try:
            emitter = container.resolve(EventEmitter, module_key=module_key)
        except Exception:
            return
        for _, handler in inspect.getmembers(instance, predicate=callable):
            event = getattr(handler, "__fanest_event__", None)
            if event is not None:
                emitter.on(event, handler)

    @staticmethod
    def _register_queue_processors(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        from fanest.queues import QueueService

        queue = getattr(instance.__class__, "__fanest_queue__", None)
        if queue is None:
            return
        try:
            queue_service = container.resolve(QueueService, module_key=module_key)
        except Exception:
            return
        for _, handler in inspect.getmembers(instance, predicate=callable):
            job_name = getattr(handler, "__fanest_process__", None)
            if job_name is not None:
                queue_service.register_processor(queue, job_name, handler)

    @staticmethod
    def _register_graphql_resolver(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        from fanest.graphql import GraphQLSchema

        if getattr(instance.__class__, "__fanest_provider__", None) is None:
            return
        has_graphql_handlers = any(
            getattr(handler, "__fanest_graphql__", None) is not None
            for _, handler in inspect.getmembers(instance, predicate=callable)
        )
        if not has_graphql_handlers:
            return
        try:
            schema = container.resolve(GraphQLSchema, module_key=module_key)
        except Exception:
            return
        schema.register_resolver(instance)

    @staticmethod
    def _register_cqrs_handlers(container: FaNestContainer, instance: object, *, module_key: Any | None = None) -> None:
        from fanest.cqrs import CommandBus, EventBus, QueryBus

        command = getattr(instance.__class__, "__fanest_command_handler__", None)
        if command is not None:
            try:
                container.resolve(CommandBus, module_key=module_key).register(command, instance)
            except Exception:
                pass
        query = getattr(instance.__class__, "__fanest_query_handler__", None)
        if query is not None:
            try:
                container.resolve(QueryBus, module_key=module_key).register(query, instance)
            except Exception:
                pass
        for event in getattr(instance.__class__, "__fanest_event_handlers__", []):
            try:
                container.resolve(EventBus, module_key=module_key).register(event, instance)
            except Exception:
                pass

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
        from fanest.workers import WorkerService

        try:
            workers = container.resolve(WorkerService, module_key=module_key)
        except Exception:
            return
        for _, handler in inspect.getmembers(instance, predicate=callable):
            task_name = getattr(handler, "__fanest_task_handler__", None)
            if task_name is not None:
                workers.register(task_name, handler)
