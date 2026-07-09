from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import asyncio
import time
from types import SimpleNamespace

from fanest import BackgroundTasks, Catch, Controller, FaNestFactory, Get, Module, Req, Session, UseFilters
from fanest.health import HealthCheckError, HealthIndicator, HealthModule, HealthService, MemoryHealthIndicator
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

    @Get("/clear")
    async def clear_session(self, session=Session()):
        session.clear()
        return {"cleared": True}


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


def test_session_clear_deletes_server_side_session_and_expires_cookie():
    SHARED_SESSION_STORE.sessions.clear()
    client = TestClient(FaNestFactory.create(SharedSessionModule))
    cookie = client.get("/session/set").cookies.get("session")
    assert cookie is not None
    assert SHARED_SESSION_STORE.sessions

    response = client.get("/session/clear", cookies={"session": cookie})

    assert response.json() == {"cleared": True}
    assert SHARED_SESSION_STORE.sessions == {}
    assert response.headers["set-cookie"].lower().find("max-age=0") >= 0


def test_session_module_rejects_unsafe_same_site_none_without_secure():
    try:
        SessionModule.for_root(secret_key="test-session-secret", same_site="none")
    except ValueError as exc:
        assert "https_only" in str(exc)
    else:
        raise AssertionError("same_site='none' without https_only should fail")


def test_session_module_rejects_invalid_same_site_policy():
    try:
        SessionModule.for_root(secret_key="test-session-secret", same_site="wide-open")
    except ValueError as exc:
        assert "same_site" in str(exc)
    else:
        raise AssertionError("invalid same_site policy should fail")


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


@Module(
    imports=[
        HealthModule.register(
            indicators=[
                HealthIndicator("db", lambda: (_ for _ in ()).throw(HealthCheckError("db", "down"))),
                HealthIndicator("cache", lambda: (_ for _ in ()).throw(RuntimeError("secret"))),
            ],
            error_status_code=500,
            include_error_messages=False,
        )
    ]
)
class ExceptionHealthModule:
    pass


def test_health_module_reports_indicator_failures_without_leaking_messages():
    response = TestClient(FaNestFactory.create(ExceptionHealthModule)).get("/health")

    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "details": {
            "db": {"status": "error"},
            "cache": {"status": "error"},
        },
    }


@Module(
    imports=[
        HealthModule.register(
            indicators=[HealthIndicator("slow", lambda: asyncio.sleep(0.05))],
            timeout_seconds=0.001,
        )
    ]
)
class TimeoutHealthModule:
    pass


def test_health_module_marks_timed_out_indicators_unhealthy():
    response = TestClient(FaNestFactory.create(TimeoutHealthModule)).get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "details": {"slow": {"status": "error", "error": "timeout"}},
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


def test_health_module_runs_indicators_concurrently():
    service = HealthService(
        indicators=[
            HealthIndicator("one", lambda: asyncio.sleep(0.03)),
            HealthIndicator("two", lambda: asyncio.sleep(0.03)),
        ]
    )

    start = time.monotonic()
    result = asyncio.run(service.check())
    elapsed = time.monotonic() - start

    assert result == {
        "status": "ok",
        "details": {"one": {"status": "ok"}, "two": {"status": "ok"}},
    }
    assert elapsed < 0.055


async def async_health_options():
    return {
        "error_status_code": 502,
        "indicators": [HealthIndicator("search", lambda: {"status": "error", "reason": "down"})],
    }


@Module(imports=[HealthModule.register_async(use_factory=async_health_options)])
class AsyncHealthModule:
    pass


def test_health_module_supports_async_registration_and_ping_helper():
    response = TestClient(FaNestFactory.create(AsyncHealthModule)).get("/health")

    assert response.status_code == 502
    assert response.json()["details"]["search"] == {"status": "error", "reason": "down"}

    service = HealthService()
    assert service.ping_check("redis") == {"redis": {"status": "ok"}}
