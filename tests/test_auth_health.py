from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, UseGuards
from fanest.auth import AuthModule, CurrentUser, JwtAuthGuard, JwtService
from fanest.health import (
    DiskHealthIndicator,
    HealthIndicator,
    HealthModule,
    HealthService,
    HttpHealthIndicator,
    MemoryHealthIndicator,
)


@Controller("profile")
class ProfileController:
    @UseGuards(JwtAuthGuard)
    @Get("/")
    async def profile(self, user: dict = CurrentUser()):
        return {"user": user}


@Module(
    imports=[
        AuthModule.for_root(secret="test-secret-value-with-enough-entropy"),
        HealthModule.register(),
    ],
    controllers=[ProfileController],
)
class AuthAppModule:
    pass


def test_jwt_auth_guard_and_current_user():
    app = FaNestFactory.create(AuthAppModule)
    client = TestClient(app)
    token = app.state.fanest_container.resolve(JwtService).sign({"sub": "123"})

    unauthorized = client.get("/profile")
    authorized = client.get("/profile", headers={"authorization": f"Bearer {token}"})

    assert unauthorized.status_code == 401
    assert authorized.json()["user"]["sub"] == "123"


def test_health_module_registers_health_endpoint():
    app = FaNestFactory.create(AuthAppModule)
    response = TestClient(app).get("/health")

    assert response.json() == {"status": "ok"}


RichHealthModule = HealthModule.register(
    [
        HealthIndicator("database", lambda: {"status": "ok"}),
        DiskHealthIndicator(path="."),
        MemoryHealthIndicator(rss_threshold_mb=10_000),
    ]
)


def test_health_module_runs_reusable_indicators():
    app = FaNestFactory.create(RichHealthModule)
    payload = TestClient(app).get("/health").json()

    assert payload["status"] == "ok"
    assert payload["details"]["database"]["status"] == "ok"
    assert payload["details"]["disk"]["status"] == "ok"
    assert payload["details"]["memory"]["status"] == "ok"


@Module(
    imports=[
        HealthModule.register(
            [
                HealthIndicator("database", lambda: {"status": "ok"}, tags=("readiness",)),
                HealthIndicator("loop", lambda: {"status": "ok"}, tags=("liveness",)),
            ]
        )
    ]
)
class ProbeHealthModule:
    pass


def test_health_module_exposes_readiness_liveness_and_manual_readiness_state():
    app = FaNestFactory.create(ProbeHealthModule)
    service = app.state.fanest_container.resolve(HealthService)
    client = TestClient(app)

    assert client.get("/health/ready").json() == {
        "status": "ok",
        "details": {"database": {"status": "ok"}},
    }
    assert client.get("/health/live").json() == {
        "status": "ok",
        "details": {"loop": {"status": "ok"}},
    }

    service.mark_not_ready()
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["details"]["readiness"]["error"] == "application is not ready"


def test_health_service_readiness_lifecycle_hooks_and_custom_path_unsupported():
    import anyio

    service = HealthService([HealthIndicator("database", lambda: {"status": "ok"}, tags=("ready",))])

    async def exercise():
        await service.on_application_bootstrap()
        assert await service.readiness() == {
            "status": "ok",
            "details": {"database": {"status": "ok"}},
        }
        await service.before_application_shutdown()
        result = await service.readiness()
        assert result["status"] == "error"
        assert result["details"]["readiness"]["error"] == "application is not ready"

    anyio.run(exercise)

    import pytest

    with pytest.raises(NotImplementedError, match="/health/ready"):
        HealthModule.register(readiness_path="/readyz")


def test_http_health_indicator_reports_status_code(monkeypatch):
    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("fanest.health.module.request.urlopen", lambda *args, **kwargs: Response())
    indicator = HttpHealthIndicator("upstream", url="https://api.test/health", expected_status=(204,))

    import anyio

    result = anyio.run(indicator.run)

    assert result == {
        "upstream": {
            "status": "ok",
            "url": "https://api.test/health",
            "status_code": 204,
        }
    }


def test_http_health_indicator_runs_blocking_urlopen_off_the_event_loop(monkeypatch):
    import anyio
    import threading

    seen: dict[str, threading.Thread] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(*args, **kwargs):
        seen["thread"] = threading.current_thread()
        return Response()

    async def exercise():
        seen["loop_thread"] = threading.current_thread()
        indicator = HttpHealthIndicator("upstream", url="https://api.test/health", expected_status=(200,))
        return await indicator.run()

    monkeypatch.setattr("fanest.health.module.request.urlopen", fake_urlopen)

    result = anyio.run(exercise)

    # The blocking urlopen must not execute on the event loop thread.
    assert seen["thread"] is not seen["loop_thread"]
    assert result["upstream"]["status"] == "ok"


def test_http_health_indicator_treats_expected_non_2xx_status_as_healthy(monkeypatch):
    from email.message import Message
    from urllib.error import HTTPError

    def raising_urlopen(*args, **kwargs):
        raise HTTPError("https://api.test/health", 503, "unavailable", Message(), None)

    monkeypatch.setattr("fanest.health.module.request.urlopen", raising_urlopen)
    indicator = HttpHealthIndicator("upstream", url="https://api.test/health", expected_status=(503,))

    import anyio

    result = anyio.run(indicator.run)

    assert result == {
        "upstream": {
            "status": "ok",
            "url": "https://api.test/health",
            "status_code": 503,
        }
    }


def test_health_register_async_invokes_user_factory_once():
    import anyio

    calls = {"count": 0}

    def factory():
        calls["count"] += 1
        return {"indicators": [HealthIndicator("database", lambda: {"status": "ok"})]}

    module = HealthModule.register_async(use_factory=factory)

    @Module(imports=[module])
    class AsyncHealthApp:
        pass

    async def exercise():
        app = await FaNestFactory.create_async(AsyncHealthApp)
        service = await app.state.fanest_container.resolve_async(HealthService)
        return await service.check()

    result = anyio.run(exercise)

    assert calls["count"] == 1
    assert result["details"]["database"]["status"] == "ok"
