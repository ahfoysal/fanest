import fnmatch
import os
from types import SimpleNamespace
from typing import Any

import pytest

from fanest.cache import RedisCacheStore
from fanest.microservices import ClientProxy, MicroserviceRemoteError, RedisTransport
from fanest.mongodb import MotorCollection
from fanest.queues import Job, RedisStreamQueueBackend
from fanest.session import FaNestSessionMiddleware, RedisSessionStore
from fanest.throttler import RedisThrottlerStore


class FakeRedisPipeline:
    def __init__(self, client: "FakeRedisClient") -> None:
        self.client = client
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> "FakeRedisPipeline":
        self.commands.append(("zremrangebyscore", (key, minimum, maximum)))
        return self

    def zcard(self, key: str) -> "FakeRedisPipeline":
        self.commands.append(("zcard", (key,)))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> "FakeRedisPipeline":
        self.commands.append(("zadd", (key, mapping)))
        return self

    def expire(self, key: str, ttl: int) -> "FakeRedisPipeline":
        self.commands.append(("expire", (key, ttl)))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for name, args in self.commands:
            if name == "zremrangebyscore":
                key, minimum, maximum = args
                members = self.client.zsets.setdefault(key, {})
                removed = [member for member, score in members.items() if minimum <= score <= maximum]
                for member in removed:
                    members.pop(member, None)
                results.append(len(removed))
            elif name == "zcard":
                (key,) = args
                results.append(len(self.client.zsets.get(key, {})))
            elif name == "zadd":
                key, mapping = args
                self.client.zsets.setdefault(key, {}).update(mapping)
                results.append(len(mapping))
            elif name == "expire":
                key, ttl = args
                self.client.expirations[key] = ttl
                results.append(True)
        return results


class FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.expirations: dict[str, int | None] = {}
        self.deleted: list[str] = []
        self.zsets: dict[str, dict[str, float]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._sequence = 0

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.values[key] = value
        self.expirations[key] = ex

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)
        self.streams.pop(key, None)

    def scan_iter(self, *, match: str) -> list[str]:
        keys = [*self.values, *self.streams]
        return [key for key in keys if fnmatch.fnmatch(key, match)]

    def pipeline(self) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True

    def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    def xrange(self, stream: str) -> list[tuple[str, dict[str, str]]]:
        return list(self.streams.get(stream, []))


class FakeAsyncRedisClient:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.deleted: list[str] = []
        self.pinged = False
        self.closed = False
        self.reply_payload: dict[str, str] | None = None
        self._sequence = 0

    async def ping(self) -> None:
        self.pinged = True

    async def aclose(self) -> None:
        self.closed = True

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def xread(self, streams: dict[str, str], *, block: int = 0, count: int = 1) -> list[Any]:
        stream = next(iter(streams))
        if self.reply_payload is not None:
            return [(stream, [("reply-1", self.reply_payload)])]
        return []

    async def delete(self, stream: str) -> None:
        self.deleted.append(stream)
        self.streams.pop(stream, None)


def test_redis_cache_store_contract_uses_namespace_ttl_and_clear() -> None:
    redis = FakeRedisClient()
    store = RedisCacheStore(prefix="fanest:test:cache:", client=redis)

    store.set("user:1", {"name": "Ada"}, ttl=30)
    redis.set("other:user:1", '{"name":"Grace"}')

    assert store.get("user:1") == {"name": "Ada"}
    assert redis.expirations["fanest:test:cache:user:1"] == 30

    store.clear()

    assert "fanest:test:cache:user:1" not in redis.values
    assert redis.values["other:user:1"] == '{"name":"Grace"}'


def test_redis_session_store_contract_round_trips_json_and_ttl() -> None:
    redis = FakeRedisClient()
    store = RedisSessionStore(prefix="fanest:test:session:", client=redis)

    store.save("session-1", {"user_id": 7}, max_age=120)

    assert store.load("session-1") == {"user_id": 7}
    assert redis.expirations["fanest:test:session:session-1"] == 120


def test_session_middleware_with_external_store_rotates_invalid_cookie() -> None:
    store = RedisSessionStore(prefix="fanest:test:session:", client=FakeRedisClient())
    middleware = FaNestSessionMiddleware(lambda *_: None, secret_key="secret", store=store)

    session_id, session, had_cookie = middleware._load_session(
        {"headers": [(b"cookie", b"session=tampered.invalid-signature")]}
    )

    assert session_id is not None
    assert session == {}
    assert had_cookie is True


def test_redis_throttler_store_contract_counts_with_expiring_zsets() -> None:
    redis = FakeRedisClient()
    store = RedisThrottlerStore(prefix="fanest:test:throttle:", client=redis)

    assert store.hit("client-1", limit=2, ttl=60) is True
    assert store.hit("client-1", limit=2, ttl=60) is True
    assert store.hit("client-1", limit=2, ttl=60) is False
    assert redis.expirations["fanest:test:throttle:client-1"] == 60


@pytest.mark.anyio
async def test_redis_microservice_transport_contract_accepts_supplied_client() -> None:
    redis = FakeAsyncRedisClient()
    redis.reply_payload = {"data": '{"ok": true}', "error": "", "error_type": ""}
    transport = RedisTransport(prefix="fanest:test:microservice:", client=redis)
    client = ClientProxy(transport)

    assert await client.send("remote.ping", {"id": 1}) == {"ok": True}
    await client.close()

    assert redis.pinged is True
    assert redis.closed is True
    assert redis.streams["fanest:test:microservice:requests"][0][1]["pattern"] == "remote.ping"
    assert redis.deleted


@pytest.mark.anyio
async def test_redis_microservice_transport_contract_raises_remote_error_envelopes() -> None:
    redis = FakeAsyncRedisClient()
    redis.reply_payload = {"data": "null", "error": "boom", "error_type": "ValueError"}
    client = ClientProxy(RedisTransport(prefix="fanest:test:microservice:", client=redis))

    with pytest.raises(MicroserviceRemoteError) as exc_info:
        await client.send("remote.fail", {})

    assert str(exc_info.value) == "boom"
    assert exc_info.value.error_type == "ValueError"


@pytest.mark.anyio
async def test_redis_stream_queue_backend_contract_persists_and_clears_jobs() -> None:
    redis = FakeRedisClient()
    backend = RedisStreamQueueBackend(prefix="fanest:test:queue:", client=redis)
    job = Job(id="job-1", queue="emails", name="welcome", data={"email": "ada@example.com"}, attempts=1)

    await backend.add(job)

    [stored] = backend.jobs("emails")
    assert stored.id == job.id
    assert stored.queue == "emails"
    assert stored.name == "welcome"
    assert stored.data == {"email": "ada@example.com"}
    assert backend.jobs()[0].metadata == {}

    backend.clear()

    assert redis.streams == {}
    assert "fanest:test:queue:emails" in redis.deleted


class FakeMotorCursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def sort(self, sort: str | list[tuple[str, int]] | tuple[str, int], direction: int | None = None):
        fields = [(sort, direction or 1)] if isinstance(sort, str) else ([sort] if isinstance(sort, tuple) else sort)
        for field, order in reversed(fields):
            self.documents.sort(key=lambda item, field=field: str(item.get(field, "")), reverse=order == -1)
        return self

    def skip(self, count: int) -> "FakeMotorCursor":
        self.documents = self.documents[count:]
        return self

    def limit(self, count: int) -> "FakeMotorCursor":
        self.documents = self.documents[:count]
        return self

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._index >= len(self.documents):
            raise StopAsyncIteration
        value = self.documents[self._index]
        self._index += 1
        return value


class FakeMotorCollection:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []

    async def insert_one(self, document: dict[str, Any]) -> None:
        self.documents.append(dict(document))

    async def insert_many(self, documents: list[dict[str, Any]]) -> None:
        self.documents.extend(dict(document) for document in documents)

    def find(self, query: dict[str, Any], *, projection: Any = None) -> FakeMotorCursor:
        documents = [dict(document) for document in self.documents if self._matches(document, query)]
        if projection is not None:
            included = {field for field, enabled in projection.items() if enabled}
            excluded = {field for field, enabled in projection.items() if not enabled}
            if included:
                documents = [{field: document[field] for field in included if field in document} for document in documents]
            for document in documents:
                for field in excluded:
                    document.pop(field, None)
        return FakeMotorCursor(documents)

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for document in self.documents:
            if self._matches(document, query):
                return dict(document)
        return None

    async def update_one(self, query: dict[str, Any], changes: dict[str, Any]) -> SimpleNamespace:
        modified = 0
        for document in self.documents:
            if self._matches(document, query):
                for key, value in changes.get("$set", changes).items():
                    document[key] = value
                modified = 1
                break
        return SimpleNamespace(modified_count=modified)

    async def update_many(self, query: dict[str, Any], changes: dict[str, Any]) -> SimpleNamespace:
        count = 0
        for document in self.documents:
            if self._matches(document, query):
                for key, value in changes.get("$set", changes).items():
                    document[key] = value
                count += 1
        return SimpleNamespace(modified_count=count)

    async def delete_one(self, query: dict[str, Any]) -> SimpleNamespace:
        for index, document in enumerate(self.documents):
            if self._matches(document, query):
                del self.documents[index]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, query: dict[str, Any]) -> SimpleNamespace:
        before = len(self.documents)
        self.documents = [document for document in self.documents if not self._matches(document, query)]
        return SimpleNamespace(deleted_count=before - len(self.documents))

    async def count_documents(self, query: dict[str, Any]) -> int:
        return len([document for document in self.documents if self._matches(document, query)])

    async def distinct(self, field: str, query: dict[str, Any]) -> list[Any]:
        values: list[Any] = []
        for document in self.documents:
            if self._matches(document, query) and document.get(field) not in values:
                values.append(document.get(field))
        return values

    def _matches(self, document: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(document.get(key) == value for key, value in query.items())


@pytest.mark.anyio
async def test_motor_collection_contract_matches_in_memory_collection_surface() -> None:
    collection = MotorCollection(FakeMotorCollection())

    created = await collection.insert_one({"email": "ada@example.com", "role": "admin"})
    await collection.insert_many(
        [
            {"email": "grace@example.com", "role": "operator"},
            {"email": "linus@example.com", "role": "admin"},
        ]
    )

    assert created["_id"]
    assert [item["email"] for item in await collection.find({"role": "admin"}, sort=("email", -1), limit=1)] == [
        "linus@example.com"
    ]
    assert await collection.count_documents({"role": "admin"}) == 2
    assert await collection.distinct("role") == ["admin", "operator"]

    updated = await collection.update_one({"email": "ada@example.com"}, {"name": "Ada"})
    assert updated is not None
    assert updated["name"] == "Ada"

    assert await collection.update_many({"role": "admin"}, {"role": "owner"}) == 2
    assert await collection.delete_one({"email": "grace@example.com"}) is True
    assert await collection.delete_many({"role": "owner"}) == 2
    assert await collection.count_documents({}) == 0


@pytest.mark.live_redis
@pytest.mark.anyio
async def test_live_redis_contracts_are_opt_in() -> None:
    redis_url = os.getenv("FANEST_LIVE_REDIS_URL")
    if not redis_url:
        pytest.skip("Set FANEST_LIVE_REDIS_URL to run live Redis integration contracts.")

    store = RedisCacheStore(url=redis_url, prefix="fanest:live-test:cache:")
    store.set("ping", {"ok": True}, ttl=5)
    assert store.get("ping") == {"ok": True}
    store.clear()


@pytest.mark.live_mongo
@pytest.mark.anyio
async def test_live_mongo_contracts_are_opt_in() -> None:
    mongo_url = os.getenv("FANEST_LIVE_MONGO_URL")
    if not mongo_url:
        pytest.skip("Set FANEST_LIVE_MONGO_URL to run live Mongo integration contracts.")

    motor_module = pytest.importorskip("motor.motor_asyncio")
    client = motor_module.AsyncIOMotorClient(mongo_url)
    collection = MotorCollection(client["fanest_live_test"]["contracts"])
    await collection.clear()
    created = await collection.insert_one({"email": "live@example.com"})
    assert await collection.find_one({"_id": created["_id"]}) == created
    await collection.clear()
    client.close()
