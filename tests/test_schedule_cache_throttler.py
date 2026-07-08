import time

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module, UseGuards, UseInterceptors
from fanest.cache import CacheInterceptor, CacheModule, CacheTTL
from fanest.schedule import Cron, Interval
from fanest.throttler import Throttle, ThrottlerGuard, ThrottlerModule


@Injectable()
class JobsService:
    interval_runs = 0
    cron_runs = 0

    @Interval(0.01)
    async def interval_job(self):
        type(self).interval_runs += 1

    @Cron("*/1 * * * * *")
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


@Controller("cached")
@UseInterceptors(CacheInterceptor)
class CachedController:
    calls = 0

    @CacheTTL(60)
    @Get("/")
    async def index(self):
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


@Controller("limited")
@UseGuards(ThrottlerGuard)
class LimitedController:
    @Throttle(limit=1, ttl=60)
    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(imports=[ThrottlerModule.for_root(limit=10, ttl=60)], controllers=[LimitedController])
class LimitedModule:
    pass


def test_throttler_guard_blocks_after_limit():
    client = TestClient(FaNestFactory.create(LimitedModule))

    assert client.get("/limited").status_code == 200
    assert client.get("/limited").status_code == 429
