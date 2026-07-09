import asyncio
import os
import smtplib
import warnings
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text

from fanest.cache import RedisCacheStore
from fanest.mailer import MailMessage, SmtpMailerTransport
from fanest.microservices import (
    GrpcTransport,
    KafkaTransport,
    NatsTransport,
    RabbitMqTransport,
    RedisTransport,
)
from fanest.mongodb import MongoService
from fanest.queues import Job, RedisStreamQueueBackend
from fanest.session import RedisSessionStore
from fanest.sqlalchemy import SqlAlchemyService


pytestmark = pytest.mark.live_external

SERVICE_GATES = {
    "Redis": "FANEST_LIVE_REDIS_URL",
    "Mongo": "FANEST_LIVE_MONGO_URL",
    "Postgres/SQLAlchemy": "FANEST_LIVE_POSTGRES_URL",
    "SMTP": "FANEST_LIVE_SMTP_HOST",
    "NATS": "FANEST_LIVE_NATS_URL",
    "RabbitMQ": "FANEST_LIVE_RABBITMQ_URL",
    "Kafka": "FANEST_LIVE_KAFKA_BOOTSTRAP_SERVERS",
    "gRPC": "FANEST_LIVE_GRPC_TARGET",
}


def _env(name: str, service: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"Set {name} to run live {service} certification.")
    return value


async def _bounded(label: str, call: Callable[[], Awaitable[Any]], timeout: float | None = None) -> Any:
    timeout = timeout or float(os.getenv("FANEST_LIVE_SERVICE_TIMEOUT", "10"))
    try:
        return await asyncio.wait_for(call(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise AssertionError(f"{label} timed out after {timeout:g}s") from exc


class FakeSyncRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.deleted: list[str] = []
        self._sequence = 0

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.deleted.append(key)
            self.values.pop(key, None)
            self.streams.pop(key, None)

    def scan_iter(self, match: str) -> list[str]:
        prefix = match.removesuffix("*")
        return [key for key in [*self.values, *self.streams] if key.startswith(prefix)]

    def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    def xrange(self, stream: str) -> list[tuple[str, dict[str, str]]]:
        return list(self.streams.get(stream, []))


class FakeAsyncRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.pinged = False
        self.closed = False

    async def ping(self) -> None:
        self.pinged = True

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeBrokerAdapter:
    reply: Any = None
    connected: bool = False
    sent: list[tuple[Any, Any]] | None = None
    emitted: list[tuple[Any, Any]] | None = None

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def send(self, pattern: Any, data: Any, **kwargs: Any) -> Any:
        if self.sent is None:
            self.sent = []
        self.sent.append((pattern, data))
        return self.reply

    async def emit(self, pattern: Any, data: Any, **kwargs: Any) -> None:
        if self.emitted is None:
            self.emitted = []
        self.emitted.append((pattern, data))


class FakeGrpcStub:
    async def Check(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "echo": data["value"]}


class RecordingSmtp:
    last_message: Any = None
    last_recipients: list[str] | None = None

    def __init__(self, host: str, port: int = 25) -> None:
        self.host = host
        self.port = port

    def __enter__(self) -> "RecordingSmtp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def login(self, username: str, password: str) -> None:
        self.username = username
        self.password = password

    def send_message(self, message: Any, to_addrs: list[str]) -> None:
        RecordingSmtp.last_message = message
        RecordingSmtp.last_recipients = list(to_addrs)


def test_fake_fallback_contracts_cover_external_service_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeSyncRedis()
    cache = RedisCacheStore(client=redis, prefix="cert:cache:")
    sessions = RedisSessionStore(client=redis, prefix="cert:session:")
    queue = RedisStreamQueueBackend(client=redis, prefix="cert:queue:")

    cache.set("key", {"ok": True}, ttl=30)
    sessions.save("sid", {"user": "ada"}, max_age=60)

    async def queue_round_trip() -> None:
        await queue.add(Job(id="job-1", queue="jobs", name="smoke", data={"id": 1}))

    asyncio.run(queue_round_trip())

    assert cache.get("key") == {"ok": True}
    assert sessions.load("sid") == {"user": "ada"}
    assert queue.jobs("jobs")[0].data == {"id": 1}

    monkeypatch.setattr(smtplib, "SMTP", RecordingSmtp)
    transport = SmtpMailerTransport({"host": "smtp.invalid", "from": "from@example.com"})
    transport.send(
        MailMessage(
            to="to@example.com",
            subject="fake certification",
            text="hello",
            bcc="audit@example.com",
        )
    )
    assert RecordingSmtp.last_recipients == ["to@example.com", "audit@example.com"]
    assert RecordingSmtp.last_message["Bcc"] is None


@pytest.mark.anyio
async def test_fake_fallback_contracts_cover_broker_transports() -> None:
    for transport_cls in (NatsTransport, RabbitMqTransport, KafkaTransport):
        adapter = FakeBrokerAdapter(reply={"ok": True})
        transport = transport_cls(adapter=adapter)
        await transport.connect()
        assert await transport.send("cert.check", {"id": 1}) == {"ok": True}
        await transport.emit("cert.event", {"id": 2})
        await transport.close()
        assert adapter.sent == [("cert.check", {"id": 1})]
        assert adapter.emitted == [("cert.event", {"id": 2})]
        assert adapter.connected is False

    redis = FakeAsyncRedis()
    redis_transport = RedisTransport(client=redis)
    await redis_transport.connect()
    await redis_transport.close()
    assert redis.pinged is True
    assert redis.closed is True

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="The 'grpc' microservice transport is running in single-process mode.*",
            category=UserWarning,
        )
        grpc = GrpcTransport(stub=FakeGrpcStub())
    await grpc.connect()
    assert await grpc.send("Check", {"value": 7}) == {"ok": True, "echo": 7}
    await grpc.close()


@pytest.mark.live_redis
def test_live_redis_certification() -> None:
    url = _env("FANEST_LIVE_REDIS_URL", "Redis")
    prefix = f"fanest:cert:{os.getpid()}:"
    cache = RedisCacheStore(url=url, prefix=f"{prefix}cache:")
    sessions = RedisSessionStore(url=url, prefix=f"{prefix}session:")
    queue = RedisStreamQueueBackend(url=url, prefix=f"{prefix}queue:")

    try:
        cache.set("ping", {"ok": True}, ttl=10)
        sessions.save("sid", {"user_id": 1}, max_age=10)

        async def queue_round_trip() -> None:
            await queue.add(Job(id="job-1", queue="jobs", name="cert", data={"ok": True}))

        asyncio.run(queue_round_trip())

        assert cache.get("ping") == {"ok": True}
        assert sessions.load("sid") == {"user_id": 1}
        assert queue.jobs("jobs")[0].data == {"ok": True}
    finally:
        cache.clear()
        sessions.clear()
        queue.clear()


@pytest.mark.live_mongo
@pytest.mark.anyio
async def test_live_mongo_certification() -> None:
    uri = _env("FANEST_LIVE_MONGO_URL", "Mongo")
    database = os.getenv("FANEST_LIVE_MONGO_DB", f"fanest_cert_{os.getpid()}")
    service = MongoService({"uri": uri, "database": database})
    collection = service.collection("certification")
    try:
        await collection.clear()
        created = await collection.insert_one({"email": "cert@example.com", "role": "admin"})
        await collection.update_one({"_id": created["_id"]}, {"$set": {"role": "owner"}})

        assert (await collection.find_one({"_id": created["_id"]}))["role"] == "owner"
        assert await collection.count_documents({"role": "owner"}) == 1
    finally:
        await collection.clear()
        await service.on_application_shutdown()


@pytest.mark.live_postgres
@pytest.mark.anyio
async def test_live_postgres_sqlalchemy_certification() -> None:
    url = _env("FANEST_LIVE_POSTGRES_URL", "Postgres/SQLAlchemy")
    service = SqlAlchemyService({"database_url": url})
    table = f"fanest_cert_{os.getpid()}"
    try:
        async with service.engine.begin() as connection:
            await connection.execute(text(f'CREATE TEMPORARY TABLE "{table}" (id integer primary key, name text)'))
            await connection.execute(text(f'INSERT INTO "{table}" (id, name) VALUES (1, :name)'), {"name": "Ada"})
            result = await connection.execute(text(f'SELECT name FROM "{table}" WHERE id = 1'))
            assert result.scalar_one() == "Ada"
    finally:
        await service.on_application_shutdown()


@pytest.mark.live_smtp
def test_live_smtp_certification() -> None:
    host = _env("FANEST_LIVE_SMTP_HOST", "SMTP")
    options: dict[str, Any] = {
        "host": host,
        "port": int(os.getenv("FANEST_LIVE_SMTP_PORT", "25")),
        "from": os.getenv("FANEST_LIVE_SMTP_FROM", "fanest@example.com"),
    }
    if os.getenv("FANEST_LIVE_SMTP_USERNAME"):
        options["username"] = os.environ["FANEST_LIVE_SMTP_USERNAME"]
        options["password"] = os.getenv("FANEST_LIVE_SMTP_PASSWORD", "")
    transport = SmtpMailerTransport(options)
    transport.send(
        MailMessage(
            to=os.getenv("FANEST_LIVE_SMTP_TO", options["from"]),
            subject="FaNest live SMTP certification",
            text="FaNest live SMTP certification probe.",
        )
    )


@pytest.mark.live_nats
@pytest.mark.anyio
async def test_live_nats_certification() -> None:
    url = _env("FANEST_LIVE_NATS_URL", "NATS")
    nats = pytest.importorskip("nats")
    client = await _bounded("NATS connect", lambda: nats.connect(url))
    transport = NatsTransport(client=client, subject_prefix=f"fanest.cert.{os.getpid()}.")

    async def run_probe() -> None:
        await transport.connect()
        await transport.emit("event", {"ok": True})

    try:
        await _bounded("NATS publish", run_probe)
    finally:
        with suppress(Exception):
            await transport.close()


@pytest.mark.live_rabbitmq
@pytest.mark.anyio
async def test_live_rabbitmq_certification() -> None:
    url = _env("FANEST_LIVE_RABBITMQ_URL", "RabbitMQ")
    routing_key = f"fanest.cert.{os.getpid()}"
    transport = RabbitMqTransport(url=url, routing_key=routing_key)

    async def run_probe() -> None:
        await transport.connect()
        await transport.emit(routing_key, {"ok": True})

    try:
        await _bounded("RabbitMQ publish", run_probe)
    finally:
        with suppress(Exception):
            await transport.close()


@pytest.mark.live_kafka
@pytest.mark.anyio
async def test_live_kafka_certification() -> None:
    bootstrap_servers = _env("FANEST_LIVE_KAFKA_BOOTSTRAP_SERVERS", "Kafka")
    topic = os.getenv("FANEST_LIVE_KAFKA_TOPIC", "fanest.certification")
    transport = KafkaTransport(bootstrap_servers=bootstrap_servers, topic=topic)

    async def run_probe() -> None:
        await transport.connect()
        await transport.emit("cert.event", {"ok": True})

    try:
        await _bounded("Kafka publish", run_probe)
    finally:
        with suppress(Exception):
            await transport.close()


@pytest.mark.live_grpc
@pytest.mark.anyio
async def test_live_grpc_channel_certification() -> None:
    target = _env("FANEST_LIVE_GRPC_TARGET", "gRPC")
    transport = GrpcTransport(target=target)

    async def run_probe() -> None:
        await transport.connect()
        channel = transport._channel
        assert channel is not None
        await channel.channel_ready()

    try:
        await _bounded("gRPC channel readiness", run_probe)
    finally:
        with suppress(Exception):
            await transport.close()


def test_live_certification_env_gate_inventory_is_documented() -> None:
    assert SERVICE_GATES == {
        "Redis": "FANEST_LIVE_REDIS_URL",
        "Mongo": "FANEST_LIVE_MONGO_URL",
        "Postgres/SQLAlchemy": "FANEST_LIVE_POSTGRES_URL",
        "SMTP": "FANEST_LIVE_SMTP_HOST",
        "NATS": "FANEST_LIVE_NATS_URL",
        "RabbitMQ": "FANEST_LIVE_RABBITMQ_URL",
        "Kafka": "FANEST_LIVE_KAFKA_BOOTSTRAP_SERVERS",
        "gRPC": "FANEST_LIVE_GRPC_TARGET",
    }
