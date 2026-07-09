import asyncio
import fnmatch
import os
from typing import Any
from uuid import uuid4

import pytest

from fanest import Module
from fanest.cache import CacheService, RedisCacheStore
from fanest.microservices import MessagePattern, MicroserviceServer, RedisTransport
from fanest.queues import Job, QueueService, RedisStreamQueueBackend
from fanest.session import RedisSessionStore
from fanest.throttler import RedisThrottlerStore


class FakeRedisPipeline:
    def __init__(self, client: "FakeRedis"):
        self.client = client
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> "FakeRedisPipeline":
        self.commands.append(("zremrangebyscore", (key, minimum, maximum)))
        return self

    def zcard(self, key: str) -> "FakeRedisPipeline":
        self.commands.append(("zcard", (key,)))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for command, args in self.commands:
            results.append(getattr(self.client, command)(*args))
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.deleted: list[str] = []
        self.eval_calls = 0
        self.closed = False
        self._sequence = 0

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.values[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            self.deleted.append(key)
            removed += int(key in self.values or key in self.streams or key in self.zsets)
            self.values.pop(key, None)
            self.streams.pop(key, None)
            self.zsets.pop(key, None)
        return removed

    def scan_iter(self, match: str):
        keys = {*self.values, *self.streams, *self.zsets}
        for key in sorted(keys):
            if fnmatch.fnmatch(key, match):
                yield key

    def pipeline(self) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)

    def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> int:
        zset = self.zsets.setdefault(key, {})
        removed = [member for member, score in zset.items() if minimum <= score <= maximum]
        for member in removed:
            zset.pop(member, None)
        return len(removed)

    def zcard(self, key: str) -> int:
        return len(self.zsets.setdefault(key, {}))

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True

    def eval(self, script: str, key_count: int, key: str, *args: Any) -> int:
        self.eval_calls += 1
        now = float(args[0])
        window_start = float(args[1])
        member = str(args[2])
        limit = int(args[3])
        ttl = int(args[4])
        self.zremrangebyscore(key, 0, window_start)
        if self.zcard(key) >= limit:
            self.expire(key, ttl)
            return 0
        self.zadd(key, {member: now})
        self.expire(key, ttl)
        return 1

    def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    def xrange(self, stream: str):
        return list(self.streams.get(stream, []))

    def close(self) -> None:
        self.closed = True


class FakeAsyncRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.deleted: list[str] = []
        self.pinged = False
        self.closed = False
        self._sequence = 0

    async def ping(self) -> None:
        self.pinged = True

    async def close(self) -> None:
        self.closed = True

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def xread(self, streams: dict[str, str], *, block: int = 0, count: int = 1):
        for stream, last_id in streams.items():
            messages = [
                (message_id, fields)
                for message_id, fields in self.streams.get(stream, [])
                if self._after(message_id, last_id)
            ]
            if messages:
                return [(stream, messages[:count])]
        return []

    async def delete(self, stream: str) -> int:
        self.deleted.append(stream)
        self.streams.pop(stream, None)
        return 1

    def _after(self, message_id: str, last_id: str) -> bool:
        if last_id == "$":
            return False
        if last_id == "0-0":
            return True
        return int(message_id.split("-", 1)[0]) > int(last_id.split("-", 1)[0])


def test_redis_cache_store_uses_json_ttl_prefix_clear_and_close():
    fake = FakeRedis()
    store = RedisCacheStore(prefix="test:cache:", client=fake)

    store.set("alpha", {"value": 1}, ttl=30)
    fake.set("other:alpha", '"untouched"')

    assert store.get("alpha") == {"value": 1}
    assert fake.expires["test:cache:alpha"] == 30

    store.set("expired", {"gone": True}, ttl=0)
    assert store.get("expired") is None

    store.clear()
    store.close()

    assert "test:cache:alpha" in fake.deleted
    assert fake.get("other:alpha") == '"untouched"'
    assert fake.closed is True


def test_cache_service_accepts_injected_redis_client_options():
    fake = FakeRedis()
    service = CacheService(
        {"redis_url": "redis://unused", "redis_prefix": "svc:", "redis_client": fake, "ttl": 5}
    )

    service.set("key", ["value"])

    assert fake.values["svc:key"] == '["value"]'
    assert fake.expires["svc:key"] == 5


def test_redis_session_store_load_save_delete_and_close():
    fake = FakeRedis()
    store = RedisSessionStore(prefix="test:session:", client=fake)

    store.save("sid", {"user": 1}, max_age=60)

    assert store.load("sid") == {"user": 1}
    assert fake.expires["test:session:sid"] == 60

    store.save("sid", {"user": 1}, max_age=0)
    store.close()

    assert store.load("sid") == {}
    assert "test:session:sid" in fake.deleted
    assert fake.closed is True


def test_redis_throttler_store_is_atomic_and_does_not_overcount_denied_hits():
    fake = FakeRedis()
    store = RedisThrottlerStore(prefix="test:throttle:", client=fake)

    assert store.hit("client", limit=1, ttl=60) is True
    assert store.hit("client", limit=1, ttl=60) is False
    assert fake.eval_calls == 2
    assert fake.zcard("test:throttle:client") == 1
    assert fake.expires["test:throttle:client"] == 60

    store.close()
    assert fake.closed is True


@pytest.mark.anyio
async def test_redis_stream_queue_backend_serializes_jobs_and_closes():
    fake = FakeRedis()
    backend = RedisStreamQueueBackend(prefix="test:queue:", client=fake)
    queue = QueueService({"backend": backend})

    job = await queue.add(
        "emails",
        {"to": "ada@example.com"},
        name="welcome",
        job_id="job-1",
        attempts=3,
        metadata={"priority": "high"},
    )

    assert backend.jobs("emails") == [job]
    assert backend.jobs("emails")[0].metadata == {"priority": "high"}
    assert queue.stats("emails").waiting == 1

    backend.clear()
    await queue.close()

    assert "test:queue:emails" in fake.deleted
    assert fake.closed is True


@pytest.mark.anyio
async def test_redis_stream_queue_backend_collapses_job_state_updates():
    fake = FakeRedis()
    backend = RedisStreamQueueBackend(prefix="test:queue:", client=fake)
    queue = QueueService({"backend": backend})
    handled: list[str] = []

    async def handler(job: Job) -> None:
        handled.append(job.id)

    queue.register_processor("emails", "welcome", handler)

    job = await queue.add("emails", {"to": "ada@example.com"}, name="welcome", job_id="job-1")

    persisted = backend.jobs("emails")
    assert handled == ["job-1"]
    assert len(fake.streams["test:queue:emails"]) == 3
    assert persisted == [queue.get_job(job.id)]
    assert persisted[0].status == "completed"


class HeaderEchoService:
    seen_headers: list[dict[str, Any]] = []

    @MessagePattern("headers.echo")
    async def echo(self, data, context):
        type(self).seen_headers.append(context.headers)
        return {"data": data, "headers": context.headers, "correlation": context.correlation_id}


@Module(providers=[HeaderEchoService])
class HeaderEchoModule:
    pass


@pytest.mark.anyio
async def test_redis_microservice_transport_processes_fake_streams_and_closes_with_close_method():
    HeaderEchoService.seen_headers = []
    fake = FakeAsyncRedis()
    transport = RedisTransport(prefix="test:micro:", client=fake)
    MicroserviceServer(HeaderEchoModule, transport=transport).compile()

    await transport.connect()
    await fake.xadd(
        "test:micro:requests",
        {
            "id": "request-1",
            "pattern": "headers.echo",
            "data": '{"ok": true}',
            "headers": '{"tenant": "acme"}',
            "reply_to": "test:micro:reply:request-1",
        },
    )

    await transport.listen_once(last_request_id="0-0")
    await transport.close()

    reply = fake.streams["test:micro:reply:request-1"][0][1]
    assert reply["error"] == ""
    assert '"tenant": "acme"' in reply["data"]
    assert HeaderEchoService.seen_headers == [{"tenant": "acme"}]
    assert fake.pinged is True
    assert fake.closed is True


@pytest.mark.anyio
async def test_redis_microservice_client_timeout_deletes_reply_stream():
    fake = FakeAsyncRedis()
    transport = RedisTransport(prefix="test:micro:", client=fake, response_timeout=0.001)

    with pytest.raises(TimeoutError):
        await transport.send("missing.remote", {"ok": True})

    assert any(stream.startswith("test:micro:reply:") for stream in fake.deleted)


@pytest.mark.live_redis
def test_live_redis_cache_session_throttler_and_queue_smoke():
    url = os.getenv("FANEST_LIVE_REDIS_URL")
    if not url:
        pytest.skip("Set FANEST_LIVE_REDIS_URL to run live Redis smoke tests.")

    redis = pytest.importorskip("redis")

    prefix = f"fanest:test:{uuid4()}:"
    client = redis.Redis.from_url(url)
    try:
        cache = RedisCacheStore(prefix=f"{prefix}cache:", client=client)
        cache.set("key", {"ok": True}, ttl=10)
        assert cache.get("key") == {"ok": True}

        session = RedisSessionStore(prefix=f"{prefix}session:", client=client)
        session.save("sid", {"user": "ada"}, max_age=10)
        assert session.load("sid") == {"user": "ada"}

        throttler = RedisThrottlerStore(prefix=f"{prefix}throttle:", client=client)
        assert throttler.hit("client", limit=1, ttl=10) is True
        assert throttler.hit("client", limit=1, ttl=10) is False

        backend = RedisStreamQueueBackend(prefix=f"{prefix}queue:", client=client)

        async def add_job() -> Job:
            return await backend.add(Job(id="job", queue="emails", name="welcome", data={"ok": True}))

        asyncio.run(add_job())
        assert backend.jobs("emails")[0].data == {"ok": True}
    finally:
        for key in client.scan_iter(match=f"{prefix}*"):
            client.delete(key)
        client.close()


@pytest.mark.live_redis
@pytest.mark.anyio
async def test_live_redis_microservice_transport_smoke():
    url = os.getenv("FANEST_LIVE_REDIS_URL")
    if not url:
        pytest.skip("Set FANEST_LIVE_REDIS_URL to run live Redis microservice smoke tests.")

    redis = pytest.importorskip("redis")

    prefix = f"fanest:test:{uuid4()}:micro:"
    server = MicroserviceServer(
        HeaderEchoModule,
        transport=RedisTransport(url=url, prefix=prefix, response_timeout=2),
    )
    client_transport = RedisTransport(url=url, prefix=prefix, response_timeout=2)
    try:
        await server.listen()
        await asyncio.sleep(0.05)
        response = await client_transport.send("headers.echo", {"live": True})

        assert response["data"] == {"live": True}
    finally:
        await client_transport.close()
        await server.close()
        cleanup = redis.Redis.from_url(url)
        try:
            for key in cleanup.scan_iter(match=f"{prefix}*"):
                cleanup.delete(key)
        finally:
            cleanup.close()
