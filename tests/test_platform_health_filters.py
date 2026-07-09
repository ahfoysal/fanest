from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from types import SimpleNamespace

from fanest import BackgroundTasks, Catch, Controller, FaNestFactory, Get, Module, Req, Session, UseFilters
from fanest.health import HealthIndicator, HealthModule, MemoryHealthIndicator
import fanest.health.module as health_module
from fanest.security import HelmetModule
from fanest.session import MemorySessionStore, SessionModule


@Catch(KeyError)
class KeyErrorFilter:
    def catch(self, exc, context):
        return {"wrong": True}


@Catch(ValueError)
class ValueErrorFilter:
    def catch(self, exc, context):
        return {"error": str(exc)}


@Controller("filters")
@UseFilters(KeyErrorFilter, ValueErrorFilter)
class FilterController:
    @Get("/")
    async def index(self):
        raise ValueError("typed")


@Module(controllers=[FilterController])
class FilterModule:
    pass


def test_catch_decorator_limits_exception_filter_scope():
    response = TestClient(FaNestFactory.create(FilterModule)).get("/filters")

    assert response.json() == {"error": "typed"}


@Catch(RuntimeError)
class BackgroundTaskFilter:
    def catch(self, exc, context):
        return JSONResponse({"error": str(exc)}, status_code=400)


@Controller("filtered-background")
@UseFilters(BackgroundTaskFilter)
class FilteredBackgroundController:
    tasks: list[str] = []

    @Get("/")
    async def index(self, background_tasks=BackgroundTasks()):
        background_tasks.add_task(self.tasks.append, "queued-before-error")
        raise RuntimeError("filtered")


@Module(controllers=[FilteredBackgroundController])
class FilteredBackgroundModule:
    pass


def test_background_tasks_survive_exception_filter_responses():
    FilteredBackgroundController.tasks = []
    client = TestClient(FaNestFactory.create(FilteredBackgroundModule))

    response = client.get("/filtered-background")

    assert response.status_code == 400
    assert response.json() == {"error": "filtered"}
    assert FilteredBackgroundController.tasks == ["queued-before-error"]


@Controller("session")
class SessionController:
    @Get("/set")
    async def set_session(self, req=Req()):
        req.session["user"] = "Ada"
        return {"ok": True}

    @Get("/get")
    async def get_session(self, session=Session()):
        return {"user": session.get("user")}


@Module(
    imports=[
        SessionModule.for_root(secret_key="test-session-secret"),
        HelmetModule.for_root(),
    ],
    controllers=[SessionController],
)
class PlatformModule:
    pass


SHARED_SESSION_STORE = MemorySessionStore()


@Module(
    imports=[
        SessionModule.for_root(
            secret_key="test-session-secret",
            store=SHARED_SESSION_STORE,
        )
    ],
    controllers=[SessionController],
)
class SharedSessionModule:
    pass


def test_session_and_security_header_modules():
    client = TestClient(FaNestFactory.create(PlatformModule))

    assert client.get("/session/set").json() == {"ok": True}
    response = client.get("/session/get")

    assert response.json() == {"user": "Ada"}
    assert response.headers["x-content-type-options"] == "nosniff"


def test_session_module_accepts_shared_server_side_store_for_multi_instance_sessions():
    SHARED_SESSION_STORE.sessions.clear()
    first = TestClient(FaNestFactory.create(SharedSessionModule))
    second = TestClient(FaNestFactory.create(SharedSessionModule))

    set_response = first.get("/session/set")
    assert set_response.json() == {"ok": True}
    cookie = set_response.cookies.get("session")
    assert cookie is not None

    assert second.get("/session/get", cookies={"session": cookie}).json() == {"user": "Ada"}


@Module(
    imports=[
        HealthModule.register(
            indicators=[
                HealthIndicator("db", lambda: {"status": "ok"}),
                HealthIndicator("cache", lambda: {"status": "ok"}),
            ]
        )
    ]
)
class IndicatorHealthModule:
    pass


def test_health_module_runs_named_indicators():
    response = TestClient(FaNestFactory.create(IndicatorHealthModule)).get("/health")

    assert response.json() == {
        "status": "ok",
        "details": {"db": {"status": "ok"}, "cache": {"status": "ok"}},
    }


@Module(
    imports=[
        HealthModule.register(
            indicators=[HealthIndicator("db", lambda: {"status": "error"})],
        )
    ]
)
class FailingHealthModule:
    pass


def test_health_module_returns_503_when_any_indicator_errors():
    response = TestClient(FaNestFactory.create(FailingHealthModule)).get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "details": {"db": {"status": "error"}},
    }


def test_memory_health_indicator_uses_platform_ru_maxrss_units(monkeypatch):
    monkeypatch.setattr(health_module.sys, "platform", "linux")
    monkeypatch.setattr(
        health_module.resource,
        "getrusage",
        lambda _: SimpleNamespace(ru_maxrss=2048),
    )

    assert MemoryHealthIndicator()._check()["rss_mb"] == 2

    monkeypatch.setattr(health_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        health_module.resource,
        "getrusage",
        lambda _: SimpleNamespace(ru_maxrss=2 * 1024 * 1024),
    )

    assert MemoryHealthIndicator()._check()["rss_mb"] == 2
