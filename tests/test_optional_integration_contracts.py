import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fanest.cache import RedisCacheStore
from fanest.microservices import MicroserviceRemoteError, RedisTransport
from fanest.mongodb import MongoService
from fanest.queues import QueueModule, QueueService, RedisStreamQueueBackend
from fanest.session import RedisSessionStore
from fanest.throttler import RedisThrottlerStore


class FakeRedisPipeline:
    def __init__(self, client: "FakeSyncRedis") -> None:
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

    def execute(self) -> list[int]:
        results: list[int] = []
        for name, args in self.commands:
            if name == "zremrangebyscore":
                key, minimum, maximum = args
                zset = self.client.zsets.setdefault(key, {})
                removed = [member for member, score in zset.items() if minimum <= score <= maximum]
                for member in removed:
                    zset.pop(member, None)
                results.append(len(removed))
            elif name == "zcard":
                (key,) = args
                results.append(len(self.client.zsets.setdefault(key, {})))
            elif name == "zadd":
                key, mapping = args
                self.client.zsets.setdefault(key, {}).update(mapping)
                results.append(len(mapping))
            elif name == "expire":
                results.append(1)
        return results


class FakeSyncRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
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
            self.zsets.pop(key, None)

    def scan_iter(self, match: str) -> list[str]:
        prefix = match.removesuffix("*")
        return [
            key
            for key in [*self.values, *self.streams, *self.zsets]
            if key.startswith(prefix)
        ]

    def pipeline(self) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def expire(self, key: str, ttl: int) -> bool:
        return True

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
        self.deleted: list[str] = []
        self.pinged = False
        self.closed = False
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

    async def xread(
        self,
        streams: dict[str, str],
        *,
        block: int = 0,
        count: int = 1,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        for stream, last_id in streams.items():
            messages = [
                (message_id, fields)
                for message_id, fields in self.streams.get(stream, [])
                if self._after(message_id, last_id)
            ]
            if messages:
                return [(stream, messages[:count])]
        return []

    async def delete(self, stream: str) -> None:
        self.deleted.append(stream)
        self.streams.pop(stream, None)

    def _after(self, message_id: str, last_id: str) -> bool:
        if last_id == "0-0":
            return True
        if last_id == "$":
            return False
        return int(message_id.split("-", 1)[0]) > int(last_id.split("-", 1)[0])


class FakeMotorUpdateResult:
    def __init__(self, modified_count: int) -> None:
        self.modified_count = modified_count


class FakeMotorDeleteResult:
    def __init__(self, deleted_count: int) -> None:
        self.deleted_count = deleted_count


class FakeMotorCursor:
    def __init__(self, collection: "FakeMotorCollection", query: dict[str, Any], projection: Any) -> None:
        self.collection = collection
        self.query = query
        self.projection = projection
        self.sort_value: Any = None
        self.skip_value = 0
        self.limit_value: int | None = None

    def sort(self, sort: Any, direction: int | str | None = None) -> "FakeMotorCursor":
        self.sort_value = (sort, direction) if isinstance(sort, str) and direction is not None else sort
        return self

    def skip(self, skip: int) -> "FakeMotorCursor":
        self.skip_value = skip
        return self

    def limit(self, limit: int) -> "FakeMotorCursor":
        self.limit_value = limit
        return self

    async def _documents(self) -> list[dict[str, Any]]:
        return await self.collection.memory.find(
            self.query,
            sort=self.sort_value,
            skip=self.skip_value,
            limit=self.limit_value,
            projection=self.projection,
        )

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def iterate() -> AsyncIterator[dict[str, Any]]:
            for document in await self._documents():
                yield document

        return iterate()


class FakeMotorCollection:
    def __init__(self, name: str) -> None:
        from fanest.mongodb import MongoCollection

        self.memory = MongoCollection(name)

    async def insert_one(self, document: dict[str, Any]) -> None:
        await self.memory.insert_one(document)

    async def insert_many(self, documents: list[dict[str, Any]]) -> None:
        await self.memory.insert_many(documents)

    def find(self, query: dict[str, Any], projection: Any = None) -> FakeMotorCursor:
        return FakeMotorCursor(self, query, projection)

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return await self.memory.find_one(query)

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> FakeMotorUpdateResult:
        updated = await self.memory.update_one(query, update)
        return FakeMotorUpdateResult(1 if updated else 0)

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> FakeMotorUpdateResult:
        return FakeMotorUpdateResult(await self.memory.update_many(query, update))

    async def delete_one(self, query: dict[str, Any]) -> FakeMotorDeleteResult:
        return FakeMotorDeleteResult(1 if await self.memory.delete_one(query) else 0)

    async def delete_many(self, query: dict[str, Any]) -> FakeMotorDeleteResult:
        return FakeMotorDeleteResult(await self.memory.delete_many(query))

    async def count_documents(self, query: dict[str, Any]) -> int:
        return await self.memory.count_documents(query)

    async def distinct(self, field: str, query: dict[str, Any]) -> list[Any]:
        return await self.memory.distinct(field, query)


class FakeMotorDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, FakeMotorCollection] = {}

    def __getitem__(self, name: str) -> FakeMotorCollection:
        return self.collections.setdefault(name, FakeMotorCollection(name))


class FakeMotorClient:
    def __init__(self, db: FakeMotorDatabase) -> None:
        self.db = db
        self.closed = False

    def __getitem__(self, name: str) -> FakeMotorDatabase:
        return self.db

    def close(self) -> None:
        self.closed = True


def test_redis_cache_session_and_throttler_contracts_with_fake_client() -> None:
    client = FakeSyncRedis()
    cache = RedisCacheStore(client=client, prefix="test:cache:")
    sessions = RedisSessionStore(client=client, prefix="test:session:")
    throttler = RedisThrottlerStore(client=client, prefix="test:throttle:")

    cache.set("answer", {"value": 42}, ttl=30)
    assert cache.get("answer") == {"value": 42}
    cache.delete("answer")
    assert cache.get("answer") is None

    cache.set("one", 1)
    cache.set("two", 2)
    cache.clear()
    assert cache.get("one") is None
    assert cache.get("two") is None

    sessions.save("sid", {"user_id": "ada"}, max_age=60)
    assert sessions.load("sid") == {"user_id": "ada"}

    assert throttler.hit("ip:route", limit=2, ttl=60) is True
    assert throttler.hit("ip:route", limit=2, ttl=60) is True
    assert throttler.hit("ip:route", limit=2, ttl=60) is False


@pytest.mark.anyio
async def test_redis_queue_backend_contract_with_injected_fake_client() -> None:
    client = FakeSyncRedis()
    backend = RedisStreamQueueBackend(client=client, prefix="test:queue:")
    queue = QueueService({"backend": backend})

    job = await queue.add("emails", {"email": "ada@example.com"}, name="welcome")

    assert queue.jobs("emails")[0].id == job.id
    assert queue.jobs("emails")[0].data == {"email": "ada@example.com"}
    assert client.streams["test:queue:emails"][0][1]["name"] == "welcome"

    queue.clear()
    assert queue.jobs("emails") == []


def test_queue_module_accepts_injected_redis_client_without_opening_network_connection() -> None:
    module = QueueModule.for_root(redis_client=FakeSyncRedis(), redis_prefix="test:queue:")
    options = module.__fanest_module__.providers[0].use_value
    service = QueueService(options)

    assert isinstance(service.backend, RedisStreamQueueBackend)


@pytest.mark.anyio
async def test_redis_microservice_transport_contract_with_injected_fake_client() -> None:
    client = FakeAsyncRedis()
    transport = RedisTransport(client=client, prefix="test:ms:")

    await transport.connect()
    assert client.pinged is True

    async def xread(streams: dict[str, str], *, block: int = 0, count: int = 1):
        reply_stream = next(iter(streams))
        return [
            (
                reply_stream,
                [
                    (
                        "1-0",
                        {"data": "null", "error": "boom", "error_type": "ValueError"},
                    )
                ],
            )
        ]

    client.xread = xread

    with pytest.raises(MicroserviceRemoteError) as exc_info:
        await transport.send("remote.fail", {"id": 1})

    assert exc_info.value.error_type == "ValueError"
    assert client.deleted == ["test:ms:reply:" + client.streams["test:ms:requests"][0][1]["id"]]

    await transport.close()
    assert client.closed is True


@pytest.mark.anyio
async def test_mongo_service_contract_with_injected_motor_style_database() -> None:
    db = FakeMotorDatabase()
    client = FakeMotorClient(db)
    service = MongoService({"client": client, "db": db})
    users = service.collection("users")

    created = await users.insert_one({"email": "ada@example.com", "profile": {"age": 36}})
    await users.update_one({"_id": created["_id"]}, {"$inc": {"profile.age": 1}})

    assert await users.find_one({"email": "ada@example.com"}) == {
        "_id": created["_id"],
        "email": "ada@example.com",
        "profile": {"age": 37},
    }
    assert await users.count_documents({"profile.age": {"$gte": 37}}) == 1

    await service.on_application_shutdown()
    assert client.closed is True


@pytest.mark.live_redis
@pytest.mark.skipif(
    not os.getenv("FANEST_LIVE_REDIS_URL"),
    reason="Set FANEST_LIVE_REDIS_URL to run optional Redis integration smoke tests.",
)
def test_live_redis_cache_store_smoke() -> None:
    url = os.environ["FANEST_LIVE_REDIS_URL"]
    cache = RedisCacheStore(url=url, prefix="fanest:test:cache:")
    cache.set("smoke", {"ok": True}, ttl=5)

    assert cache.get("smoke") == {"ok": True}

    cache.clear()


@pytest.mark.live_mongo
@pytest.mark.skipif(
    not os.getenv("FANEST_LIVE_MONGO_URL"),
    reason="Set FANEST_LIVE_MONGO_URL to run optional Mongo integration smoke tests.",
)
@pytest.mark.anyio
async def test_live_mongo_service_smoke() -> None:
    service = MongoService(
        {
            "uri": os.environ["FANEST_LIVE_MONGO_URL"],
            "database": os.getenv("FANEST_LIVE_MONGO_DB", "fanest_test"),
        }
    )
    collection = service.collection("fanest_smoke")
    await collection.clear()
    created = await collection.insert_one({"email": "smoke@example.com"})

    assert await collection.find_one({"_id": created["_id"]}) == created

    await collection.clear()
    await service.on_application_shutdown()
