import asyncio
import fnmatch
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module, UseGuards, UseInterceptors
from fanest.cache import CacheInterceptor, CacheKey, CacheModule, CacheService, CacheTTL, MemoryCacheStore, RedisCacheStore
from fanest.schedule import Cron, CronExpression, CronJob, Interval, SchedulerRegistry, Timeout
from fanest.schedule.runner import ScheduleRunner
from fanest.session import RedisSessionStore, SessionModule
from fanest.throttler import (
    MemoryThrottlerStore,
    RedisThrottlerStore,
    SkipThrottle,
    Throttle,
    ThrottlerGuard,
    ThrottlerModule,
    ThrottlerService,
)


@Injectable()
class JobsService:
    interval_runs = 0
    cron_runs = 0

    @Interval(0.01)
    async def interval_job(self):
        type(self).interval_runs += 1

    @Cron(CronExpression.EVERY_SECOND)
    async def cron_job(self):
        type(self).cron_runs += 1


@Module(providers=[JobsService])
class JobsModule:
    pass


def test_interval_and_cron_jobs_run_during_lifespan():
    JobsService.interval_runs = 0
    JobsService.cron_runs = 0

    with TestClient(FaNestFactory.create(JobsModule)):
        time.sleep(1.05)

    assert JobsService.interval_runs > 0
    assert JobsService.cron_runs > 0


def test_cron_delay_uses_full_expression_not_first_field_only():
    runner = ScheduleRunner([])
    now = datetime(2026, 7, 8, 8, 59, 0, tzinfo=timezone.utc)

    assert runner.next_cron_delay("0 9 * * *", now) == 60


@Injectable()
class TimeoutJobsService:
    runs = 0

    @Timeout(0.01, name="warmup")
    async def warmup(self):
        type(self).runs += 1


@Controller("scheduler")
class SchedulerController:
    def __init__(self, registry: SchedulerRegistry):
        self.registry = registry

    @Get("/")
    async def index(self):
        return {"jobs": [job.name for job in self.registry.list()]}


@Module(providers=[TimeoutJobsService], controllers=[SchedulerController])
class TimeoutModule:
    pass


def test_timeout_job_runs_once_and_registry_is_injectable():
    TimeoutJobsService.runs = 0

    with TestClient(FaNestFactory.create(TimeoutModule)) as client:
        assert client.get("/scheduler").json() == {"jobs": ["warmup"]}
        time.sleep(0.04)
        assert TimeoutJobsService.runs == 1


def test_scheduler_registry_exposes_nest_style_typed_methods():
    async def idle():
        await asyncio.sleep(0)

    async def run():
        registry = SchedulerRegistry()
        cron = asyncio.create_task(idle())
        interval = asyncio.create_task(idle())
        timeout = asyncio.create_task(idle())
        registry.add_cron_job("cron", cron, {"expression": CronExpression.EVERY_SECOND})
        registry.add_interval("interval", interval)
        registry.add_timeout("timeout", timeout)

        assert registry.get_cron_job("cron").metadata["expression"] == CronExpression.EVERY_SECOND
        assert registry.get_intervals() == ["interval"]
        assert registry.get_timeouts() == ["timeout"]
        assert list(registry.get_cron_jobs()) == ["cron"]
        registry.delete_cron_job("cron")
        registry.delete_interval("interval")
        registry.delete_timeout("timeout")
        assert registry.list() == []

    asyncio.run(run())


@Injectable()
class DisabledCronService:
    runs = 0

    @CronJob(CronExpression.EVERY_SECOND, disabled=True)
    async def disabled(self):
        type(self).runs += 1


@Module(providers=[DisabledCronService])
class DisabledCronModule:
    pass


def test_disabled_cron_job_is_not_scheduled():
    DisabledCronService.runs = 0

    with TestClient(FaNestFactory.create(DisabledCronModule)):
        time.sleep(0.03)

    assert DisabledCronService.runs == 0


@Injectable()
class FlakyIntervalService:
    runs = 0

    @Interval(0.01)
    async def flaky(self):
        type(self).runs += 1
        if type(self).runs == 1:
            raise RuntimeError("temporary failure")


@Module(providers=[FlakyIntervalService])
class FlakyIntervalModule:
    pass


def test_scheduled_job_exception_does_not_kill_repeating_task():
    FlakyIntervalService.runs = 0

    with TestClient(FaNestFactory.create(FlakyIntervalModule)):
        time.sleep(0.05)

    assert FlakyIntervalService.runs > 1


@Injectable()
class SlowIntervalService:
    started: list[float] = []

    @Interval(0.02)
    async def slow(self):
        type(self).started.append(time.monotonic())
        await asyncio.sleep(0.04)


@Module(providers=[SlowIntervalService])
class SlowIntervalModule:
    pass


def test_interval_jobs_do_not_drift_by_waiting_for_slow_handlers():
    SlowIntervalService.started = []

    with TestClient(FaNestFactory.create(SlowIntervalModule)):
        time.sleep(0.075)

    assert len(SlowIntervalService.started) >= 3


class InfiniteIntervalService:
    @Interval(60)
    async def interval(self):
        pass


@pytest.mark.anyio
async def test_schedule_runner_stop_cancels_infinite_interval_loops():
    runner = ScheduleRunner([InfiniteIntervalService()])
    runner.start()

    await asyncio.wait_for(runner.stop(), timeout=0.25)

    assert runner.tasks == []


class FakeRedisPipeline:
    def __init__(self, client):
        self.client = client
        self.commands = []

    def zremrangebyscore(self, key, minimum, maximum):
        self.commands.append(("zremrangebyscore", key, minimum, maximum))
        return self

    def zcard(self, key):
        self.commands.append(("zcard", key))
        return self

    def zadd(self, key, mapping):
        self.commands.append(("zadd", key, mapping))
        return self

    def expire(self, key, ttl):
        self.commands.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for command in self.commands:
            name = command[0]
            if name == "zremrangebyscore":
                _, key, minimum, maximum = command
                zset = self.client.zsets.get(key, {})
                self.client.zsets[key] = {
                    member: score for member, score in zset.items() if not minimum <= score <= maximum
                }
                results.append(1)
            elif name == "zcard":
                _, key = command
                results.append(len(self.client.zsets.get(key, {})))
            elif name == "zadd":
                _, key, mapping = command
                self.client.zsets.setdefault(key, {}).update(mapping)
                results.append(len(mapping))
            elif name == "expire":
                _, key, ttl = command
                self.client.expirations[key] = ttl
                results.append(True)
        return results


class FakeRedisClient:
    def __init__(self):
        self.values: dict[str, bytes] = {}
        self.expirations: dict[str, int | None] = {}
        self.deleted: list[str] = []
        self.zsets: dict[str, dict[str, float]] = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value.encode() if isinstance(value, str) else value
        self.expirations[key] = ex
        return True

    def delete(self, *keys):
        for key in keys:
            decoded = key.decode() if isinstance(key, bytes) else key
            self.deleted.append(decoded)
            self.values.pop(decoded, None)
            self.zsets.pop(decoded, None)
        return len(keys)

    def scan_iter(self, match):
        return [key.encode() for key in self.values if fnmatch.fnmatch(key, match)]

    def pipeline(self):
        return FakeRedisPipeline(self)

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def expire(self, key, ttl):
        self.expirations[key] = ttl
        return True


@Controller("cached")
@UseInterceptors(CacheInterceptor)
class CachedController:
    calls = 0

    @CacheTTL(60)
    @Get("/")
    async def index(self):
        type(self).calls += 1
        return {"calls": type(self).calls}

    @CacheTTL(60)
    @Get("/secure")
    async def secure(self):
        type(self).calls += 1
        return {"calls": type(self).calls}

    @CacheKey("cached:custom")
    @CacheTTL(60)
    @Get("/custom")
    async def custom(self):
        type(self).calls += 1
        return {"calls": type(self).calls}


@Module(imports=[CacheModule.register()], controllers=[CachedController])
class CachedModule:
    pass


def test_cache_interceptor_reuses_response():
    CachedController.calls = 0
    client = TestClient(FaNestFactory.create(CachedModule))

    assert client.get("/cached").json() == {"calls": 1}
    assert client.get("/cached").json() == {"calls": 1}


def test_cache_interceptor_varies_authenticated_requests():
    CachedController.calls = 0
    client = TestClient(FaNestFactory.create(CachedModule))

    assert client.get("/cached/secure", headers={"authorization": "Bearer one"}).json() == {"calls": 1}
    assert client.get("/cached/secure", headers={"authorization": "Bearer one"}).json() == {"calls": 1}
    assert client.get("/cached/secure", headers={"authorization": "Bearer two"}).json() == {"calls": 2}


def test_custom_cache_key_keeps_query_variance():
    CachedController.calls = 0
    client = TestClient(FaNestFactory.create(CachedModule))

    assert client.get("/cached/custom", params={"page": "1"}).json() == {"calls": 1}
    assert client.get("/cached/custom", params={"page": "1"}).json() == {"calls": 1}
    assert client.get("/cached/custom", params={"page": "2"}).json() == {"calls": 2}


def test_cache_service_isolated_per_application_instance():
    first = FaNestFactory.create(CachedModule)
    second = FaNestFactory.create(CachedModule)

    first.state.fanest_container.resolve(CacheService).set("shared", "first")
    second.state.fanest_container.resolve(CacheService).set("shared", "second")

    assert first.state.fanest_container.resolve(CacheService).get("shared") == "first"
    assert second.state.fanest_container.resolve(CacheService).get("shared") == "second"


def test_cache_module_accepts_custom_store():
    store = MemoryCacheStore()

    @Module(imports=[CacheModule.register(store=store)])
    class CustomCacheModule:
        pass

    app = FaNestFactory.create(CustomCacheModule)
    cache = app.state.fanest_container.resolve(CacheService)
    cache.set("key", "value")

    assert store.get("key") == "value"


def test_cache_service_remember_and_has_use_default_ttl():
    app = FaNestFactory.create(CachedModule)
    cache = app.state.fanest_container.resolve(CacheService)
    cache.clear()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        return {"value": calls}

    async def run():
        first = await cache.remember("remembered", load)
        second = await cache.remember("remembered", load)
        return first, second

    first, second = asyncio.run(run())

    assert first == {"value": 1}
    assert second == {"value": 1}
    assert calls == 1
    assert cache.has("remembered") is True


class AsyncCacheStore:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value, ttl: int | None = None):
        self.values[key] = value

    async def delete(self, key: str):
        self.deleted.append(key)
        self.values.pop(key, None)

    async def clear(self):
        self.values.clear()


def test_cache_service_and_interceptor_support_async_stores():
    store = AsyncCacheStore()

    @Module(imports=[CacheModule.register(store=store)])
    class AsyncStoreCacheModule:
        pass

    app = FaNestFactory.create(AsyncStoreCacheModule)
    cache = app.state.fanest_container.resolve(CacheService)

    async def run():
        await cache.set_async("key", "value")
        assert await cache.get_async("key") == "value"
        assert await cache.has_async("key") is True
        remembered = await cache.remember("remembered", lambda: {"ok": True})
        await cache.delete_async("key")
        await cache.clear_async()
        return remembered

    assert asyncio.run(run()) == {"ok": True}
    assert store.deleted == ["key"]


def test_redis_cache_store_serializes_ttl_and_clears_only_prefixed_keys():
    client = FakeRedisClient()
    store = RedisCacheStore(prefix="fanest:test-cache:", client=client)
    client.set("other:key", json.dumps({"keep": True}))

    store.set("profile", {"name": "Ada"}, ttl=30)
    store.set("expired", {"name": "Grace"}, ttl=0)

    assert store.get("profile") == {"name": "Ada"}
    assert client.expirations["fanest:test-cache:profile"] == 30
    assert store.get("expired") is None

    store.clear()

    assert client.get("fanest:test-cache:profile") is None
    other_raw = client.get("other:key")
    assert other_raw is not None
    assert json.loads(other_raw.decode()) == {"keep": True}


def test_redis_session_store_handles_bytes_and_module_client_hook():
    client = FakeRedisClient()
    store = RedisSessionStore(prefix="fanest:test-session:", client=client)

    store.save("session-1", {"user_id": "ada"}, max_age=120)

    assert store.load("session-1") == {"user_id": "ada"}
    assert client.expirations["fanest:test-session:session-1"] == 120

    @Module(
        imports=[
            SessionModule.for_root(
                secret_key="secret",
                redis_client=client,
                redis_prefix="fanest:test-session:",
            )
        ]
    )
    class RedisSessionAppModule:
        pass

    app = FaNestFactory.create(RedisSessionAppModule)
    middleware = app.user_middleware[0]

    assert cast(Any, middleware.kwargs["store"]).load("session-1") == {"user_id": "ada"}


async def async_cache_options():
    return {"ttl": None}


@Module(imports=[CacheModule.for_root_async(use_factory=async_cache_options)])
class AsyncCacheModule:
    pass


def test_cache_module_supports_async_registration_alias():
    app = FaNestFactory.create(AsyncCacheModule)
    cache = asyncio.run(app.state.fanest_container.resolve_async(CacheService))

    cache.set("key", "value")

    assert cache.get("key") == "value"
    assert cache.default_ttl is None


@Controller("limited")
@UseGuards(ThrottlerGuard)
class LimitedController:
    @Throttle(limit=1, ttl=60)
    @Get("/")
    async def index(self):
        return {"ok": True}

    @Throttle(limit=1, ttl=60)
    @Get("/other")
    async def other(self):
        return {"other": True}


@Module(imports=[ThrottlerModule.for_root(limit=10, ttl=60)], controllers=[LimitedController])
class LimitedModule:
    pass


SHARED_THROTTLER_STORE = MemoryThrottlerStore()


@Module(
    imports=[ThrottlerModule.for_root(limit=1, ttl=60, store=SHARED_THROTTLER_STORE)],
    controllers=[LimitedController],
)
class SharedStoreLimitedModule:
    pass


def test_throttler_guard_blocks_after_limit():
    client = TestClient(FaNestFactory.create(LimitedModule))

    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 429


def test_throttler_guard_uses_separate_buckets_per_route():
    client = TestClient(FaNestFactory.create(LimitedModule))

    assert client.get("/limited").status_code == 200
    assert client.get("/limited/other").status_code == 200
    assert client.get("/limited").status_code == 429
    assert client.get("/limited/other").status_code == 429


def test_throttler_module_accepts_shared_store_for_multi_instance_limits():
    SHARED_THROTTLER_STORE._hits.clear()
    first = TestClient(FaNestFactory.create(SharedStoreLimitedModule))
    second = TestClient(FaNestFactory.create(SharedStoreLimitedModule))

    assert first.get("/limited").status_code == 200
    assert second.get("/limited").status_code == 429


def test_redis_throttler_store_uses_atomic_sorted_set_window():
    client = FakeRedisClient()
    store = RedisThrottlerStore(prefix="fanest:test-throttle:", client=client)

    assert store.hit("user-1", limit=2, ttl=60) is True
    assert store.hit("user-1", limit=2, ttl=60) is True
    assert store.hit("user-1", limit=2, ttl=60) is False
    assert client.expirations["fanest:test-throttle:user-1"] == 60


def test_throttler_module_passes_redis_client_hook_to_service():
    client = FakeRedisClient()

    @Module(imports=[ThrottlerModule.for_root(limit=1, ttl=60, redis_client=client)])
    class RedisThrottlerAppModule:
        pass

    app = FaNestFactory.create(RedisThrottlerAppModule)
    service = app.state.fanest_container.resolve(ThrottlerService)

    assert service.hit("module-hook") is True
    assert service.hit("module-hook") is False


@Controller("tracked")
@UseGuards(ThrottlerGuard)
class TrackedController:
    @Throttle(limit=1, ttl=60)
    @Get("/")
    async def index(self):
        return {"ok": True}

    @SkipThrottle()
    @Throttle(limit=1, ttl=60)
    @Get("/open")
    async def open(self):
        return {"open": True}


def tracker_from_header(context):
    return context.request.headers.get("x-api-key", "anonymous")


@Module(
    imports=[ThrottlerModule.for_root(limit=1, ttl=60, get_tracker=tracker_from_header)],
    controllers=[TrackedController],
)
class TrackedModule:
    pass


def test_throttler_supports_custom_trackers_and_skip_decorator():
    client = TestClient(FaNestFactory.create(TrackedModule))

    assert client.get("/tracked", headers={"x-api-key": "one"}).status_code == 200
    assert client.get("/tracked", headers={"x-api-key": "one"}).status_code == 429
    assert client.get("/tracked", headers={"x-api-key": "two"}).status_code == 200
    assert client.get("/tracked/open", headers={"x-api-key": "one"}).status_code == 200
    assert client.get("/tracked/open", headers={"x-api-key": "one"}).status_code == 200


@pytest.mark.live_redis
@pytest.mark.skipif(not os.getenv("FANEST_LIVE_REDIS"), reason="set FANEST_LIVE_REDIS to run live Redis checks")
def test_live_redis_cache_session_and_throttler_when_enabled():
    url = os.getenv("FANEST_LIVE_REDIS_URL", "redis://localhost:6379/0")
    cache = RedisCacheStore(url=url, prefix="fanest:live-cache:")
    session = RedisSessionStore(url=url, prefix="fanest:live-session:")
    throttler = RedisThrottlerStore(url=url, prefix="fanest:live-throttle:")

    try:
        cache.clear()
        cache.set("key", {"ok": True}, ttl=30)
        assert cache.get("key") == {"ok": True}

        session.save("session-1", {"user": "ada"}, max_age=30)
        assert session.load("session-1") == {"user": "ada"}

        assert throttler.hit("user-1", limit=1, ttl=30) is True
        assert throttler.hit("user-1", limit=1, ttl=30) is False
    finally:
        cache.clear()
        cache.close()
