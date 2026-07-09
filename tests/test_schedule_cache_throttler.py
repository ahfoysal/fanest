import asyncio
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module, UseGuards, UseInterceptors
from fanest.cache import CacheInterceptor, CacheKey, CacheModule, CacheService, CacheTTL, MemoryCacheStore
from fanest.schedule import Cron, CronExpression, CronJob, Interval, SchedulerRegistry, Timeout
from fanest.schedule.runner import ScheduleRunner
from fanest.throttler import Throttle, ThrottlerGuard, ThrottlerModule


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
