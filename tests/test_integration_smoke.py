import asyncio
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from fanest import (
    Catch,
    Controller,
    FaNestFactory,
    Get,
    Injectable,
    Module,
    UseFilters,
    UseGuards,
    UseInterceptors,
    UsePipes,
)
from fanest.cache import RedisCacheStore
from fanest.cli.main import app as cli_app
from fanest.graphql import GraphQLModule, Query, Resolver
from fanest.health import HealthIndicator, HealthModule
from fanest.logger import Logger, LoggerModule
from fanest.mailer import MailAttachment, MailerModule, MailerService, SmtpMailerTransport
from fanest.microservices import (
    ClientProxy,
    EventPattern,
    KafkaTransport,
    MessagePattern,
    MicroserviceTransportError,
    MicroserviceServer,
    NatsTransport,
    RabbitMqTransport,
    RedisTransport,
    Transport,
)
from fanest.mongodb import MongoModule, MongoService
from fanest.queues import QueueService, RedisStreamQueueBackend
from fanest.session import FaNestSessionMiddleware, RedisSessionStore
from fanest.sqlalchemy import SqlAlchemyModule, SqlAlchemyService
from fanest.throttler import RedisThrottlerStore


def _skip_without_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"set {name} to run this live integration smoke")
    return value


class FakeRedisPipeline:
    def __init__(self, redis: "FakeSyncRedis") -> None:
        self.redis = redis
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    def zremrangebyscore(self, key: str, start: float, stop: float) -> "FakeRedisPipeline":
        self.commands.append(("zremrangebyscore", (key, start, stop)))
        return self

    def zcard(self, key: str) -> "FakeRedisPipeline":
        self.commands.append(("zcard", (key,)))
        return self

    def zadd(self, key: str, values: dict[str, float]) -> "FakeRedisPipeline":
        self.commands.append(("zadd", (key, values)))
        return self

    def expire(self, key: str, ttl: int) -> "FakeRedisPipeline":
        self.commands.append(("expire", (key, ttl)))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for command, args in self.commands:
            results.append(getattr(self.redis, command)(*args))
        return results


class FakeSyncRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.expirations: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex

    def delete(self, *keys: str | bytes) -> int:
        removed = 0
        for key in keys:
            normalized = self._key(key)
            removed += int(self.values.pop(normalized, None) is not None)
            removed += int(self.streams.pop(normalized, None) is not None)
            removed += int(self.sorted_sets.pop(normalized, None) is not None)
        return removed

    def scan_iter(self, match: str) -> list[bytes]:
        prefix = match[:-1] if match.endswith("*") else match
        keys = [*self.values, *self.streams, *self.sorted_sets]
        return [key.encode() for key in keys if key.startswith(prefix)]

    def xadd(self, stream: str, fields: dict[str, str]) -> bytes:
        messages = self.streams.setdefault(stream, [])
        message_id = f"{len(messages) + 1}-0"
        messages.append((message_id, fields))
        return message_id.encode()

    def xrange(self, stream: str) -> list[tuple[bytes, dict[bytes, bytes]]]:
        return [
            (
                message_id.encode(),
                {key.encode(): str(value).encode() for key, value in fields.items()},
            )
            for message_id, fields in self.streams.get(stream, [])
        ]

    def pipeline(self) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)

    def zremrangebyscore(self, key: str, start: float, stop: float) -> int:
        values = self.sorted_sets.setdefault(key, {})
        removed = [member for member, score in values.items() if start <= score <= stop]
        for member in removed:
            values.pop(member, None)
        return len(removed)

    def zcard(self, key: str) -> int:
        return len(self.sorted_sets.setdefault(key, {}))

    def zadd(self, key: str, values: dict[str, float]) -> int:
        self.sorted_sets.setdefault(key, {}).update(values)
        return len(values)

    def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True

    def _key(self, key: str | bytes) -> str:
        return key.decode() if isinstance(key, bytes) else key


class FakeAsyncRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True

    async def xadd(self, stream: str, fields: dict[str, str]) -> bytes:
        messages = self.streams.setdefault(stream, [])
        message_id = f"{len(messages) + 1}-0"
        messages.append((message_id, fields))
        return message_id.encode()

    async def xread(
        self,
        streams: dict[str, str],
        *,
        block: int | None = None,
        count: int | None = None,
    ) -> list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]:
        del count
        deadline = asyncio.get_running_loop().time() + ((block or 0) / 1000)
        while True:
            for stream, last_id in streams.items():
                messages = self.streams.get(stream, [])
                unread = [
                    (message_id, fields)
                    for message_id, fields in messages
                    if self._greater_than(message_id, last_id)
                ]
                if unread:
                    message_id, fields = unread[0]
                    return [
                        (
                            stream.encode(),
                            [
                                (
                                    message_id.encode(),
                                    {
                                        key.encode(): str(value).encode()
                                        for key, value in fields.items()
                                    },
                                )
                            ],
                        )
                    ]
            if not block or asyncio.get_running_loop().time() >= deadline:
                return []
            await asyncio.sleep(0.001)

    async def delete(self, stream: str) -> int:
        return int(self.streams.pop(stream, None) is not None)

    def _greater_than(self, left: str, right: str) -> bool:
        if right == "$":
            return False
        left_major = int(left.split("-", 1)[0])
        right_major = int(right.split("-", 1)[0])
        return left_major > right_major


def test_redis_cache_session_throttler_and_queue_fakes_cover_wire_operations() -> None:
    redis = FakeSyncRedis()
    cache = RedisCacheStore(client=redis, prefix="it:cache:")
    cache.set("answer", {"value": 42}, ttl=30)
    assert cache.get("answer") == {"value": 42}
    cache.clear()
    assert cache.get("answer") is None

    sessions = RedisSessionStore(client=redis, prefix="it:session:")
    sessions.save("sid", {"user": "Ada"}, max_age=60)
    assert sessions.load("sid") == {"user": "Ada"}
    assert redis.expirations["it:session:sid"] == 60

    throttler = RedisThrottlerStore(client=redis, prefix="it:throttle:")
    assert throttler.hit("ip", limit=2, ttl=60) is True
    assert throttler.hit("ip", limit=2, ttl=60) is True
    assert throttler.hit("ip", limit=2, ttl=60) is False

    async def run_queue() -> list[Any]:
        backend = RedisStreamQueueBackend(client=redis, prefix="it:queue:")
        service = QueueService({"backend": backend})
        await service.add("emails", {"to": "ada@example.com"}, job_id="job-1")
        return [job.data for job in backend.jobs("emails")]

    assert asyncio.run(run_queue()) == [{"to": "ada@example.com"}]


def test_session_middleware_with_fake_redis_store_round_trips_between_app_instances() -> None:
    redis = FakeSyncRedis()
    store = RedisSessionStore(client=redis, prefix="it:session:")

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope["session"]["visits"] = scope["session"].get("visits", 0) + 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = FaNestSessionMiddleware(app, secret_key="secret", store=store, max_age=120)

    async def request(cookie: str | None = None) -> str:
        sent: list[dict[str, Any]] = []
        headers = [(b"cookie", f"session={cookie}".encode())] if cookie else []
        scope = {"type": "http", "headers": headers, "session": {}}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await middleware(scope, lambda: None, send)
        response_headers = sent[0]["headers"]
        set_cookie = dict(response_headers)[b"set-cookie"].decode()
        return set_cookie.split("session=", 1)[1].split(";", 1)[0]

    first_cookie = asyncio.run(request())
    second_cookie = asyncio.run(request(first_cookie))

    assert first_cookie == second_cookie
    session_id = first_cookie.split(".", 1)[0]
    assert store.load(session_id) == {"visits": 2}


def test_redis_microservice_fake_request_reply_and_event_streams() -> None:
    client_redis = FakeAsyncRedis()
    server_transport = RedisTransport(client=client_redis, prefix="it:ms:")
    client_transport = RedisTransport(client=client_redis, prefix="it:ms:")
    events: list[dict[str, Any]] = []

    @Injectable()
    class MathHandler:
        @MessagePattern("sum")
        async def sum(self, data: dict[str, int], context: Any) -> dict[str, Any]:
            return {"result": data["a"] + data["b"], "transport": context.transport}

        @EventPattern("created")
        async def created(self, data: dict[str, Any], context: Any) -> None:
            events.append({"data": data, "transport": context.transport})

    @Module(providers=[MathHandler])
    class MathModule:
        pass

    async def run() -> None:
        MicroserviceServer.create(MathModule, transport=server_transport).compile()
        await server_transport.connect()
        client = ClientProxy(client_transport)
        try:
            pending = asyncio.create_task(client.send("sum", {"a": 2, "b": 3}))
            for _ in range(20):
                if client_redis.streams.get("it:ms:requests"):
                    break
                await asyncio.sleep(0.001)
            await server_transport.listen_once(last_request_id="0-0", last_event_id="0-0")
            assert await asyncio.wait_for(pending, timeout=1) == {
                "result": 5,
                "transport": "redis",
            }

            await client.emit("created", {"id": 1})
            await server_transport.listen_once(last_request_id="$", last_event_id="0-0")
            assert events == [{"data": {"id": 1}, "transport": "redis"}]
        finally:
            await client.close()
            await server_transport.close()

    asyncio.run(run())


def test_mailer_fake_transport_and_smtp_message_builder_cover_envelope(tmp_path: Path) -> None:
    sent: list[Any] = []

    class RecordingTransport:
        def send(self, message: Any) -> None:
            sent.append(message)

    @Module(imports=[MailerModule.for_root(transport=RecordingTransport())])
    class MailModule:
        pass

    app = FaNestFactory.create(MailModule)
    mailer = app.state.fanest_container.resolve(MailerService)
    attachment_path = tmp_path / "report.txt"
    attachment_path.write_text("hello", encoding="utf-8")
    message = mailer.send(
        to=["ada@example.com"],
        cc="grace@example.com",
        bcc="ops@example.com",
        reply_to="reply@example.com",
        subject="Report",
        text="Plain",
        html="<b>Plain</b>",
        attachments=[attachment_path, MailAttachment("raw.bin", b"abc")],
    )

    assert sent == [message]
    email = SmtpMailerTransport({"host": "smtp.invalid", "from": "noreply@example.com"}).build_email(
        message
    )
    assert email["To"] == "ada@example.com"
    assert email["Cc"] == "grace@example.com"
    assert email["Reply-To"] == "reply@example.com"
    assert "ops@example.com" not in str(email)
    assert len(list(email.iter_attachments())) == 2


def test_cli_generated_project_check_and_lifespan_shutdown_smoke(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    new_result = runner.invoke(cli_app, ["new", "smoke_api"])
    assert new_result.exit_code == 0, new_result.output
    monkeypatch.chdir(tmp_path / "smoke_api")

    check_result = runner.invoke(cli_app, ["check", "main.py"])
    build_result = runner.invoke(cli_app, ["build"])

    assert check_result.exit_code == 0, check_result.output
    assert build_result.exit_code == 0, build_result.output

    sys.path.insert(0, str(tmp_path / "smoke_api"))
    try:
        import importlib

        generated_main = importlib.import_module("main")
        with TestClient(generated_main.app) as client:
            assert client.get("/").status_code == 200
    finally:
        sys.path.remove(str(tmp_path / "smoke_api"))
        sys.modules.pop("main", None)


def test_cli_project_hosts_raw_body_versioning_and_security_headers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    new_result = runner.invoke(cli_app, ["new", "edge_api"])
    assert new_result.exit_code == 0, new_result.output

    project = tmp_path / "edge_api"
    (project / "integration_app.py").write_text(
        """
from fanest import Controller, FaNestFactory, Get, Module, Post, Req, Version, VersioningType
from fanest.security import HelmetModule


@Controller("hooks")
class HooksController:
    @Post("raw")
    async def raw(self, req=Req()):
        return {
            "raw": req.raw_body.decode(),
            "state_raw": req.state.raw_body.decode(),
            "parsed": await req.json(),
        }

    @Version("2")
    @Get("versioned")
    async def versioned(self):
        return {"version": "2"}


@Module(
    imports=[HelmetModule.for_root(headers={"x-frame-options": "SAMEORIGIN"})],
    controllers=[HooksController],
)
class AppModule:
    pass


app = FaNestFactory.create(
    AppModule,
    raw_body=True,
    versioning={"type": VersioningType.HEADER, "header": "x-api-version"},
)
        """.lstrip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    check_result = runner.invoke(cli_app, ["check", "integration_app.py", "--app", "app"])
    build_result = runner.invoke(cli_app, ["build"])

    assert check_result.exit_code == 0, check_result.output
    assert build_result.exit_code == 0, build_result.output

    sys.path.insert(0, str(project))
    try:
        import importlib

        generated_main = importlib.import_module("main")
        integration_app = importlib.import_module("integration_app")
        with TestClient(generated_main.app) as generated_client:
            assert generated_client.get("/").status_code == 200
        with TestClient(integration_app.app) as client:
            raw = client.post("/hooks/raw", json={"event": "created"})
            versioned = client.get("/hooks/versioned", headers={"x-api-version": "2"})
            missing_version = client.get("/hooks/versioned")
    finally:
        sys.path.remove(str(project))
        for module_name in ("main", "integration_app"):
            sys.modules.pop(module_name, None)

    assert raw.json() == {
        "raw": '{"event":"created"}',
        "state_raw": '{"event":"created"}',
        "parsed": {"event": "created"},
    }
    assert raw.headers["x-frame-options"] == "SAMEORIGIN"
    assert raw.headers["x-content-type-options"] == "nosniff"
    assert versioned.json() == {"version": "2"}
    assert missing_version.status_code == 404


def test_graphql_di_health_and_logger_context_smoke() -> None:
    stream = io.StringIO()

    @Injectable()
    class GreetingService:
        def __init__(self, logger: Logger):
            self.logger = logger

        def hello(self) -> dict[str, str]:
            with self.logger.bind_context(trace_id="gql-smoke"):
                self.logger.log("resolved greeting", operation="hello")
            return {"message": "hello from DI"}

    @Module(providers=[GreetingService], exports=[GreetingService])
    class GreetingModule:
        pass

    @Resolver
    class GreetingResolver:
        def __init__(self, greetings: GreetingService):
            self.greetings = greetings

        @Query()
        async def hello(self):
            return self.greetings.hello()

    @Module(
        imports=[
            LoggerModule.register(
                level=logging.INFO,
                context="Smoke",
                structured=True,
                include_timestamp=False,
                stream=stream,
                extra={"service": "fanest"},
                is_global=True,
            ),
            HealthModule.register(
                [
                    HealthIndicator(
                        "graphql",
                        lambda: {"status": "ok", "source": "di"},
                        tags=["readiness", "liveness"],
                    )
                ],
                is_global=True,
            ),
            GraphQLModule.for_root(
                imports=[GreetingModule],
                resolvers=[GreetingResolver],
                path="ops/graphql",
            ),
        ]
    )
    class OpsModule:
        pass

    with TestClient(FaNestFactory.create(OpsModule)) as client:
        graphql = client.post("/ops/graphql", json={"query": "{ hello { message } }"})
        readiness = client.get("/health/ready")
        liveness = client.get("/health/live")

    assert graphql.json() == {"data": {"hello": {"message": "hello from DI"}}}
    assert readiness.json() == {
        "status": "ok",
        "details": {"graphql": {"status": "ok", "source": "di"}},
    }
    assert liveness.status_code == 200
    log_payload = json.loads(stream.getvalue())
    assert log_payload == {
        "level": "info",
        "message": "resolved greeting",
        "context": "Smoke",
        "service": "fanest",
        "trace_id": "gql-smoke",
        "operation": "hello",
    }


def test_microservice_message_scope_runs_enhancer_chain_smoke() -> None:
    class HeaderGuard:
        def can_activate(self, context):
            return context.request.headers.get("x-allow") == "yes"

    class TitlePipe:
        def transform(self, value, metadata):
            assert metadata["source"] == "message"
            return str(value).title()

    class EnvelopeInterceptor:
        async def intercept(self, context, call_next):
            result = await call_next()
            return {
                "result": result,
                "transport": context.request.transport,
                "pattern": context.request.pattern,
            }

    @Catch(MicroserviceTransportError)
    class DeniedFilter:
        def catch(self, exc, context):
            return {"error": str(exc), "pattern": context.request.pattern}

    @UseFilters(DeniedFilter)
    class WorkflowHandler:
        @MessagePattern("workflow.run")
        @UseGuards(HeaderGuard)
        @UsePipes(TitlePipe())
        @UseInterceptors(EnvelopeInterceptor())
        async def run(self, data, context):
            return {"name": data, "headers": dict(context.headers)}

    @Module(providers=[WorkflowHandler, HeaderGuard, DeniedFilter])
    class WorkflowModule:
        pass

    server = MicroserviceServer(WorkflowModule).compile()

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        allowed = await server.transport._dispatch_message(
            "workflow.run",
            "ada lovelace",
            headers={"x-allow": "yes", "x-trace": "smoke"},
        )
        denied = await server.client().send("workflow.run", "grace hopper")
        return allowed, denied

    allowed, denied = asyncio.run(run())

    assert allowed == {
        "result": {
            "name": "Ada Lovelace",
            "headers": {"x-allow": "yes", "x-trace": "smoke"},
        },
        "transport": "memory",
        "pattern": "workflow.run",
    }
    assert denied == {"error": "Forbidden", "pattern": "workflow.run"}


def test_asgi_lifespan_startup_and_shutdown_calls_hooks() -> None:
    events: list[str] = []

    @Injectable()
    class LifecycleProbe:
        async def on_module_init(self) -> None:
            events.append("init")

        async def on_application_bootstrap(self) -> None:
            events.append("bootstrap")

        async def before_application_shutdown(self) -> None:
            events.append("before_shutdown")

        async def on_module_destroy(self) -> None:
            events.append("destroy")

        async def on_application_shutdown(self) -> None:
            events.append("shutdown")

    @Controller()
    class ProbeController:
        @Get()
        async def index(self) -> dict[str, bool]:
            return {"ok": True}

    @Module(providers=[LifecycleProbe], controllers=[ProbeController])
    class ProbeModule:
        pass

    with TestClient(FaNestFactory.create(ProbeModule)) as client:
        assert client.get("/").json() == {"ok": True}
        assert events == ["init", "bootstrap"]

    assert events == ["init", "bootstrap", "destroy", "before_shutdown", "shutdown"]


def test_package_build_smoke_writes_dist_to_temp_directory(tmp_path: Path) -> None:
    if importlib.util.find_spec("hatchling") is None:
        pytest.skip("hatchling is not installed locally; skipping offline package build smoke")
    dist_dir = tmp_path / "dist"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--sdist",
            "--wheel",
            "--outdir",
            str(dist_dir),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout
    assert list(dist_dir.glob("fanest-*.tar.gz"))
    assert list(dist_dir.glob("fanest-*.whl"))


@pytest.mark.wheel_smoke
def test_wheel_installs_and_cli_imports_in_temp_venv(tmp_path: Path) -> None:
    if os.getenv("FANEST_LIVE_WHEEL") != "1":
        pytest.skip("set FANEST_LIVE_WHEEL=1 to build and install the wheel in a temp venv")
    dist_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=120,
    )
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=120)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    fanest = venv / ("Scripts/fanest.exe" if os.name == "nt" else "bin/fanest")
    wheel = next(dist_dir.glob("fanest-*.whl"))
    subprocess.run([str(python), "-m", "pip", "install", str(wheel)], check=True, timeout=180)
    result = subprocess.run(
        [str(fanest), "info"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout
    assert "FaNest" in result.stdout
    new_result = subprocess.run(
        [str(fanest), "new", "wheel_api"],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    assert new_result.returncode == 0, new_result.stdout
    project = tmp_path / "wheel_api"
    for command in (["check", "main.py"], ["build"]):
        project_result = subprocess.run(
            [str(fanest), *command],
            cwd=project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        assert project_result.returncode == 0, project_result.stdout
    app_result = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib, sys\n"
                "from fastapi.testclient import TestClient\n"
                f"sys.path.insert(0, {str(project)!r})\n"
                "main = importlib.import_module('main')\n"
                "with TestClient(main.app) as client:\n"
                "    assert client.get('/').status_code == 200\n"
            ),
        ],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    assert app_result.returncode == 0, app_result.stdout


@pytest.mark.live_redis
def test_live_redis_cache_session_throttler_queue_smoke() -> None:
    redis_url = _skip_without_env("FANEST_LIVE_REDIS_URL")
    prefix = f"fanest:it:{os.getpid()}:"
    cache = RedisCacheStore(url=redis_url, prefix=f"{prefix}cache:")
    sessions = RedisSessionStore(url=redis_url, prefix=f"{prefix}session:")
    throttler = RedisThrottlerStore(url=redis_url, prefix=f"{prefix}throttle:")
    queue_backend = RedisStreamQueueBackend(url=redis_url, prefix=f"{prefix}queue:")
    try:
        cache.set("key", {"ok": True}, ttl=30)
        assert cache.get("key") == {"ok": True}
        sessions.save("sid", {"user": "Ada"}, max_age=30)
        assert sessions.load("sid") == {"user": "Ada"}
        assert throttler.hit("ip", limit=1, ttl=30) is True
        assert throttler.hit("ip", limit=1, ttl=30) is False

        async def add_job() -> None:
            service = QueueService({"backend": queue_backend})
            await service.add("jobs", {"id": 1}, job_id="live-job")

        asyncio.run(add_job())
        assert [job.data for job in queue_backend.jobs("jobs")] == [{"id": 1}]
    finally:
        cache.clear()
        queue_backend.clear()


@pytest.mark.live_mongo
@pytest.mark.anyio
async def test_live_mongo_motor_smoke() -> None:
    mongo_url = _skip_without_env("FANEST_LIVE_MONGO_URL")
    database = os.getenv("FANEST_LIVE_MONGO_DB", f"fanest_it_{os.getpid()}")
    app = FaNestFactory.create(MongoModule.for_root(uri=mongo_url, database=database))
    service = await app.state.fanest_container.resolve_async(MongoService)
    collection = service.collection("users")
    try:
        inserted = await collection.insert_one({"name": "Ada", "role": "admin"})
        assert inserted["_id"] is not None
        assert await collection.find_one({"_id": inserted["_id"]}) == inserted
    finally:
        await collection.clear()
        await service.on_application_shutdown()


@pytest.mark.live_postgres
@pytest.mark.anyio
async def test_live_postgres_sqlalchemy_lifecycle_smoke() -> None:
    postgres_url = _skip_without_env("FANEST_LIVE_POSTGRES_URL")
    app = FaNestFactory.create(SqlAlchemyModule.for_root(database_url=postgres_url))
    service = await app.state.fanest_container.resolve_async(SqlAlchemyService)
    async with service.engine.begin() as connection:
        result = await connection.exec_driver_sql("SELECT 1")
        assert result.scalar_one() == 1
    await service.on_application_shutdown()


@pytest.mark.live_smtp
def test_live_smtp_mailer_smoke() -> None:
    host = _skip_without_env("FANEST_LIVE_SMTP_HOST")
    port = int(os.getenv("FANEST_LIVE_SMTP_PORT", "25"))
    sender = os.getenv("FANEST_LIVE_SMTP_FROM", "fanest@example.com")
    to = os.getenv("FANEST_LIVE_SMTP_TO", sender)
    username = os.getenv("FANEST_LIVE_SMTP_USERNAME")
    password = os.getenv("FANEST_LIVE_SMTP_PASSWORD", "")
    smtp: dict[str, Any] = {"host": host, "port": port, "from": sender}
    if username:
        smtp.update({"username": username, "password": password})
    app = FaNestFactory.create(MailerModule.for_root(smtp=smtp, outbox=False))
    mailer = app.state.fanest_container.resolve(MailerService)
    mailer.send(to=to, subject="FaNest live SMTP smoke", text="FaNest SMTP integration smoke")


@dataclass
class RecordingBrokerAdapter:
    replies: dict[tuple[Any, Any], Any]
    connected: bool = False
    emitted: list[tuple[Any, Any]] | None = None

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def send(self, pattern: Any, data: Any) -> Any:
        return self.replies[(pattern, data["id"])]

    async def emit(self, pattern: Any, data: Any) -> None:
        if self.emitted is None:
            self.emitted = []
        self.emitted.append((pattern, data))


@pytest.mark.parametrize(
    ("transport", "env_name"),
    [
        (Transport.RABBITMQ, "FANEST_LIVE_RABBITMQ_URL"),
        (Transport.NATS, "FANEST_LIVE_NATS_URL"),
        (Transport.KAFKA, "FANEST_LIVE_KAFKA_BOOTSTRAP_SERVERS"),
    ],
)
def test_broker_transport_interfaces_have_fake_adapter_and_live_marker(
    transport: Transport,
    env_name: str,
) -> None:
    del env_name
    adapter = RecordingBrokerAdapter({("lookup", 7): {"ok": True}})
    client = MicroserviceServer.create(
        _EmptyBrokerModule,
        transport=transport,
        adapter=adapter,
    ).compile().client()

    async def run() -> None:
        assert await client.send("lookup", {"id": 7}) == {"ok": True}
        await client.emit("seen", {"id": 7})
        await client.close()

    asyncio.run(run())
    assert adapter.emitted == [("seen", {"id": 7})]


@Module()
class _EmptyBrokerModule:
    pass


@Injectable()
class LiveMathHandler:
    @MessagePattern("math.sum")
    async def sum(self, data: dict[str, int], context: Any) -> dict[str, Any]:
        return {"result": data["a"] + data["b"], "transport": context.transport}

    @EventPattern("math.seen")
    async def seen(self, data: dict[str, Any], context: Any) -> None:
        del data, context


@Module(providers=[LiveMathHandler])
class LiveMathModule:
    pass


@pytest.mark.live_nats
def test_live_nats_transport_request_reply_when_enabled() -> None:
    nats_url = _skip_without_env("FANEST_LIVE_NATS_URL")
    prefix = f"fanest.it.{os.getpid()}."

    async def run() -> None:
        server_transport = NatsTransport(
            url=nats_url,
            subject_prefix=prefix,
            listen_subject=f"{prefix}>",
        )
        client_transport = NatsTransport(url=nats_url, subject_prefix=prefix)
        server = MicroserviceServer.create(LiveMathModule, transport=server_transport)
        await server.listen()
        client = ClientProxy(client_transport, timeout=5)
        try:
            await asyncio.sleep(0.05)
            assert await client.send("math.sum", {"a": 2, "b": 4}) == {
                "result": 6,
                "transport": "nats",
            }
            await client.emit("math.seen", {"id": 1})
        finally:
            await client.close()
            await server.close()

    asyncio.run(run())


@pytest.mark.live_rabbitmq
def test_live_rabbitmq_transport_request_reply_when_enabled() -> None:
    rabbit_url = _skip_without_env("FANEST_LIVE_RABBITMQ_URL")
    exchange = f"fanest.it.{os.getpid()}"
    queue = f"{exchange}.math"

    async def run() -> None:
        server_transport = RabbitMqTransport(
            url=rabbit_url,
            exchange=exchange,
            queue=queue,
            routing_key="math.sum",
        )
        client_transport = RabbitMqTransport(url=rabbit_url, exchange=exchange)
        server = MicroserviceServer.create(LiveMathModule, transport=server_transport)
        await server.listen()
        client = ClientProxy(client_transport, timeout=5)
        try:
            await asyncio.sleep(0.05)
            assert await client.send("math.sum", {"a": 3, "b": 5}) == {
                "result": 8,
                "transport": "rabbitmq",
            }
            await client.emit("math.sum", {"a": 1, "b": 1})
        finally:
            await client.close()
            await server.close()

    asyncio.run(run())


@pytest.mark.live_kafka
def test_live_kafka_transport_connect_and_emit_when_enabled() -> None:
    bootstrap_servers = _skip_without_env("FANEST_LIVE_KAFKA_BOOTSTRAP_SERVERS")
    topic = os.getenv("FANEST_LIVE_KAFKA_TOPIC", f"fanest-it-{os.getpid()}")

    async def run() -> None:
        transport = KafkaTransport(bootstrap_servers=bootstrap_servers, topic=topic)
        await transport.connect()
        try:
            await transport.emit("math.seen", {"id": 1})
        finally:
            await transport.close()

    asyncio.run(run())
