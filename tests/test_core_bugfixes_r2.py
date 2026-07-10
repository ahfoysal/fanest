"""Regression tests for round-2 core DI / scanner / factory bug fixes."""

import asyncio

from fastapi.testclient import TestClient

from fanest import (
    Controller,
    FaNestFactory,
    Get,
    Inject,
    Injectable,
    Module,
    forward_ref,
)


def test_singleton_controller_fires_lifecycle_hooks_and_is_eager():
    events: list[str] = []

    @Controller("probe")
    class ProbeController:
        def __init__(self):
            events.append("construct")

        async def on_module_init(self):
            events.append("init")

        async def on_application_bootstrap(self):
            events.append("bootstrap")

        async def on_module_destroy(self):
            events.append("destroy")

        async def on_application_shutdown(self):
            events.append("shutdown")

        @Get("/")
        async def index(self):
            return {"ok": True}

    @Module(controllers=[ProbeController])
    class ProbeModule:
        pass

    with TestClient(FaNestFactory.create(ProbeModule)) as client:
        # Controller was constructed eagerly at bootstrap, before any request.
        assert events == ["construct", "init", "bootstrap"]
        assert client.get("/probe").json() == {"ok": True}
        # Singleton controller is not re-constructed per request.
        assert events == ["construct", "init", "bootstrap"]

    assert events == ["construct", "init", "bootstrap", "destroy", "shutdown"]


def test_request_scoped_controller_is_per_request():
    counter = {"n": 0}

    @Controller("scoped", scope="request")
    class ScopedController:
        def __init__(self):
            counter["n"] += 1

        @Get("/")
        async def index(self):
            return {"n": counter["n"]}

    @Module(controllers=[ScopedController])
    class ScopedModule:
        pass

    client = TestClient(FaNestFactory.create(ScopedModule))
    client.get("/scoped")
    client.get("/scoped")
    # Fresh controller per request.
    assert counter["n"] == 2


def test_shutdown_hooks_run_in_nest_order_destroy_before_shutdown():
    events: list[str] = []

    @Injectable()
    class Service:
        async def on_module_destroy(self):
            events.append("destroy")

        async def before_application_shutdown(self):
            events.append("before_shutdown")

        async def on_application_shutdown(self):
            events.append("shutdown")

    @Module(providers=[Service])
    class ShutdownModule:
        pass

    with TestClient(FaNestFactory.create(ShutdownModule)):
        pass

    assert events == ["destroy", "before_shutdown", "shutdown"]


def test_sync_create_with_dict_form_dynamic_module_import():
    init: list[str] = []

    @Injectable()
    class FeatureService:
        async def on_module_init(self):
            init.append("feature")

    @Module()
    class FeatureModule:
        pass

    @Module(
        imports=[
            {
                "module": FeatureModule,
                "providers": [FeatureService],
                "exports": [FeatureService],
            }
        ]
    )
    class RootModule:
        pass

    # Previously crashed at lifespan with "unhashable type: 'dict'".
    with TestClient(FaNestFactory.create(RootModule)):
        pass
    assert init == ["feature"]


def test_forward_ref_sibling_and_exported_provider_pass_boundary_validation():
    @Injectable()
    class Engine:
        power = 100

    @Injectable()
    class Car:
        def __init__(self, engine=Inject(forward_ref(lambda: Engine))):
            self.engine = engine

    @Controller("garage")
    class GarageController:
        def __init__(self, car: Car):
            self.car = car

        @Get("/")
        async def index(self):
            return {"power": self.car.engine.power}

    # Sibling provider declared via forward_ref, and the same class exported —
    # previously failed module-boundary validation ("provider is not local").
    @Module(
        controllers=[GarageController],
        providers=[Car, forward_ref(lambda: Engine)],
        exports=[forward_ref(lambda: Engine)],
    )
    class GarageModule:
        pass

    @Module(imports=[GarageModule])
    class AppModule:
        pass

    client = TestClient(FaNestFactory.create(AppModule))
    assert client.get("/garage").json() == {"power": 100}


def test_controller_with_uninspectable_callable_attribute_builds():
    @Controller("tools")
    class ToolsController:
        helper = max  # builtin: no introspectable signature

        @Get("/")
        async def index(self):
            return {"max": self.helper(1, 2)}

    @Module(controllers=[ToolsController])
    class ToolsModule:
        pass

    # Previously crashed: ValueError('no signature found for builtin ...').
    client = TestClient(FaNestFactory.create(ToolsModule))
    assert client.get("/tools").json() == {"max": 2}


def test_request_scoped_global_guard_is_instantiated_per_request():
    from fanest import use_class
    from fanest.core.enhancers import APP_GUARD

    instances: list[int] = []

    @Injectable(scope="request")
    class CountingGuard:
        def __init__(self):
            instances.append(id(self))

        def can_activate(self, context):
            return True

    @Controller("g")
    class GController:
        @Get("/")
        async def index(self):
            return {"ok": True}

    @Module(controllers=[GController], providers=[use_class(APP_GUARD, CountingGuard)])
    class GuardModule:
        pass

    app = FaNestFactory.create(GuardModule)

    async def run():
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            for _ in range(3):
                await client.get("/g")

    asyncio.run(run())
    # A fresh guard instance per request, not one shared singleton.
    assert len(set(instances)) == 3
