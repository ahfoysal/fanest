from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fanest.core.container import FaNestContainer
from fanest.core.metadata import ProviderDefinition
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
        for provider in scanner.providers:
            container.register(provider)
        for token, value in (overrides or {}).items():
            container.override(token, value)

        lifespan = FaNestFactory._lifespan(scanner.providers, container)
        app = FastAPI(
            title=title,
            version=version,
            description=description,
            debug=debug,
            lifespan=lifespan,
        )
        app.state.fanest_container = container
        app.state.fanest_root_module = root_module
        if cors:
            options = cors if isinstance(cors, dict) else {}
            app.add_middleware(
                CORSMiddleware,
                allow_origins=options.get("allow_origins", ["*"]),
                allow_credentials=options.get("allow_credentials", False),
                allow_methods=options.get("allow_methods", ["*"]),
                allow_headers=options.get("allow_headers", ["*"]),
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
            global_guards=global_guards or [],
            global_pipes=global_pipes or [],
            global_interceptors=global_interceptors or [],
            global_filters=global_filters or [],
        )
        adapter.register_controllers(scanner.controllers)
        adapter.register_gateways(scanner.gateways)
        return app

    @staticmethod
    def _lifespan(providers: list[ProviderDefinition], container: FaNestContainer):
        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            instances = []
            for provider in providers:
                instance = await container.resolve_async(container.provider_token(provider))
                instances.append(instance)
                FaNestFactory._register_events(container, instance)
                FaNestFactory._register_queue_processors(container, instance)
                hook = getattr(instance, "on_module_init", None)
                if hook is not None:
                    result = hook()
                    if hasattr(result, "__await__"):
                        await result
            schedule_runner = ScheduleRunner(instances, registry=container.resolve(SchedulerRegistry))
            schedule_runner.start()
            yield
            await schedule_runner.stop()
            for provider in providers:
                instance = await container.resolve_async(container.provider_token(provider))
                hook = getattr(instance, "on_application_shutdown", None)
                if hook is not None:
                    result = hook()
                    if hasattr(result, "__await__"):
                        await result

        return lifespan

    @staticmethod
    def _register_events(container: FaNestContainer, instance: object) -> None:
        from fanest.events import EventEmitter

        try:
            emitter = container.resolve(EventEmitter)
        except Exception:
            return
        for _, handler in inspect.getmembers(instance, predicate=callable):
            event = getattr(handler, "__fanest_event__", None)
            if event is not None:
                emitter.on(event, handler)

    @staticmethod
    def _register_queue_processors(container: FaNestContainer, instance: object) -> None:
        from fanest.queues import QueueService

        queue = getattr(instance.__class__, "__fanest_queue__", None)
        if queue is None:
            return
        try:
            queue_service = container.resolve(QueueService)
        except Exception:
            return
        for _, handler in inspect.getmembers(instance, predicate=callable):
            job_name = getattr(handler, "__fanest_process__", None)
            if job_name is not None:
                queue_service.register_processor(queue, job_name, handler)
