from fastapi.testclient import TestClient

from fanest import (
    APP_FILTER,
    APP_GUARD,
    APP_INTERCEPTOR,
    APP_PIPE,
    Controller,
    Get,
    Module,
    Query,
    use_class,
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
    async def index(self, name: str = Query()):
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
