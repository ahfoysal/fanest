from fastapi.testclient import TestClient
import pytest

from fanest import (
    APP_FILTER,
    APP_GUARD,
    APP_INTERCEPTOR,
    APP_PIPE,
    Controller,
    DynamicModule,
    Get,
    Inject,
    Injectable,
    Module,
    Query,
    dynamic_module,
    token,
    use_class,
    use_factory,
)
from fanest.testing import TestingModule


class DenyGuard:
    def can_activate(self, context):
        return False


class AllowGuard:
    def can_activate(self, context):
        return True


class LowerPipe:
    def transform(self, value, metadata):
        if metadata["name"] == "name":
            return str(value).lower()
        return value


class UpperPipe:
    def transform(self, value, metadata):
        if metadata["name"] == "name":
            return str(value).upper()
        return value


class RealInterceptor:
    async def intercept(self, context, call_next):
        result = await call_next()
        result["interceptor"] = "real"
        return result


class MockInterceptor:
    async def intercept(self, context, call_next):
        result = await call_next()
        result["interceptor"] = "mock"
        return result


class RealFilter:
    def catch(self, exc, context):
        return {"filter": "real", "error": str(exc)}


class MockFilter:
    def catch(self, exc, context):
        return {"filter": "mock", "error": str(exc)}


@Controller("testing-overrides")
class EnhancerController:
    @Get("/")
    async def index(self, name=Query()):
        return {"name": name}

    @Get("/fail")
    async def fail(self):
        raise ValueError("boom")


@Module(
    controllers=[EnhancerController],
    providers=[
        use_class(APP_GUARD, DenyGuard),
        use_class(APP_PIPE, LowerPipe),
        use_class(APP_INTERCEPTOR, RealInterceptor),
        use_class(APP_FILTER, RealFilter),
    ],
)
class EnhancerModule:
    pass


def test_testing_module_overrides_global_app_enhancers():
    app = (
        TestingModule.create(EnhancerModule)
        .override_guard()
        .use_value(AllowGuard())
        .override_pipe()
        .use_value(UpperPipe())
        .override_interceptor()
        .use_value(MockInterceptor())
        .override_filter()
        .use_value(MockFilter())
        .compile()
    )
    client = TestClient(app)

    response = client.get("/testing-overrides", params={"name": "Ada"})
    filtered = client.get("/testing-overrides/fail", params={"name": "Ada"})

    assert response.json() == {"name": "ADA", "interceptor": "mock"}
    assert filtered.json() == {"filter": "mock", "error": "boom"}


class ClassGuard:
    def can_activate(self, context):
        return False


class ReplacementGuard:
    def can_activate(self, context):
        return True


@Controller("testing-class-override")
class GuardedController:
    __fanest_guards__ = [ClassGuard]

    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(controllers=[GuardedController])
class GuardedModule:
    pass


def test_testing_module_override_guard_accepts_specific_tokens():
    app = (
        TestingModule.create(GuardedModule)
        .override_guard(ClassGuard)
        .use_value(ReplacementGuard())
        .compile()
    )

    assert TestClient(app).get("/testing-class-override").json() == {"ok": True}


@Controller("testing-controller-override")
class RealController:
    @Get("/")
    async def index(self):
        return {"controller": "real"}


class MockController:
    async def index(self):
        return {"controller": "mock"}


@Module(controllers=[RealController])
class ControllerModule:
    pass


def test_testing_module_overrides_controller_class():
    app = (
        TestingModule.create(ControllerModule)
        .override_controller(RealController)
        .use_class(MockController)
        .compile()
    )

    assert TestClient(app).get("/testing-controller-override").json() == {
        "controller": "mock"
    }


ASYNC_DYNAMIC_VALUE = token("ASYNC_DYNAMIC_VALUE")
ASYNC_OVERRIDE_VALUE = token("ASYNC_OVERRIDE_VALUE")


@Module()
class AsyncFeatureModule:
    pass


async def async_feature_import() -> DynamicModule:
    return dynamic_module(
        AsyncFeatureModule,
        providers=[use_factory(ASYNC_DYNAMIC_VALUE, lambda: "dynamic-ready")],
        exports=[ASYNC_DYNAMIC_VALUE],
    )


@Controller("testing-async-dynamic")
class AsyncDynamicController:
    def __init__(self, value: str = Inject(ASYNC_DYNAMIC_VALUE)):
        self.value = value

    @Get("/")
    async def index(self):
        return {"value": self.value}


@Module(imports=[async_feature_import()], controllers=[AsyncDynamicController])
class AsyncDynamicRootModule:
    pass


@pytest.mark.anyio
async def test_testing_module_compile_async_supports_async_dynamic_imports():
    app = await TestingModule.create(AsyncDynamicRootModule).compile_async()

    assert TestClient(app).get("/testing-async-dynamic").json() == {"value": "dynamic-ready"}


@Module(providers=[use_factory(ASYNC_OVERRIDE_VALUE, lambda: "real")], exports=[ASYNC_OVERRIDE_VALUE])
class AsyncOverrideModule:
    pass


async def async_override_factory():
    return "mock-async"


@pytest.mark.anyio
async def test_testing_module_async_override_factories_can_be_resolved():
    module = (
        TestingModule.create(AsyncOverrideModule)
        .override(ASYNC_OVERRIDE_VALUE)
        .use_factory(async_override_factory)
    )
    await module.compile_async()

    assert await module.get_async(ASYNC_OVERRIDE_VALUE) == "mock-async"
    assert await module.resolve_async(ASYNC_OVERRIDE_VALUE) == "mock-async"


def test_testing_module_reuses_created_client_until_closed():
    module = TestingModule.create(GuardedModule).override_guard(ClassGuard).use_value(ReplacementGuard())

    first = module.create_client()
    second = module.create_test_client()

    assert first is second
    assert first.get("/testing-class-override").json() == {"ok": True}
    module.close()
    assert module._app is None
    assert module._client is None


def test_testing_module_client_uses_precompiled_application_and_context_closes():
    module = TestingModule.create(GuardedModule).override_guard(ClassGuard).use_value(ReplacementGuard())
    app = module.compile()
    client = module.create_client()

    assert module.create_application() is app
    assert client.get("/testing-class-override").json() == {"ok": True}

    with module as scoped:
        assert scoped.create_application() is app

    assert module._app is None
    assert module._client is None


def test_testing_module_recompile_invalidates_existing_client():
    module = TestingModule.create(GuardedModule).override_guard(ClassGuard).use_value(ReplacementGuard())
    first_client = module.create_client()

    app = module.compile()

    assert module._client is None
    assert app is module.create_application()
    assert first_client is not module._client
    module.close()


def test_testing_module_override_after_compile_rebuilds_application():
    module = TestingModule.create(GuardedModule)
    denied = module.create_client().get("/testing-class-override")

    module.override_guard(ClassGuard).use_value(ReplacementGuard())
    allowed = module.create_client().get("/testing-class-override")

    assert denied.status_code == 403
    assert allowed.json() == {"ok": True}
    module.close()


@Injectable(scope="request")
class RequestScopedCounter:
    created = 0

    def __init__(self):
        type(self).created += 1
        self.value = type(self).created


@Module(providers=[RequestScopedCounter])
class RequestScopedTestingModule:
    pass


def test_testing_module_resolve_uses_isolated_request_scopes():
    RequestScopedCounter.created = 0
    module = TestingModule.create(RequestScopedTestingModule)

    first = module.resolve(RequestScopedCounter)
    second = module.resolve(RequestScopedCounter)

    assert first.value == 1
    assert second.value == 2
    assert first is not second
    assert module._container().current_request_instances() is None
