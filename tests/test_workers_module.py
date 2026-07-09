from fastapi.testclient import TestClient
import asyncio

import pytest

from fanest import Controller, FaNestFactory, Get, Injectable, Module
from fanest.workers import TaskHandler, WorkerModule, WorkerService, WorkerTaskNotFoundError


@Injectable()
class ReportTasks:
    @TaskHandler("reports.daily")
    async def daily(self, payload):
        return {"report": payload["name"]}


@Controller("workers")
class WorkerController:
    def __init__(self, workers: WorkerService):
        self.workers = workers

    @Get("/")
    async def index(self):
        return await self.workers.run("reports.daily", {"name": "sales"})


@Module(
    imports=[WorkerModule.for_root()],
    controllers=[WorkerController],
    providers=[ReportTasks],
)
class WorkerAppModule:
    pass


def test_worker_module_registers_task_handlers():
    app = FaNestFactory.create(WorkerAppModule)
    with TestClient(app) as client:
        assert client.get("/workers").json() == {"report": "sales"}
        workers = app.state.fanest_container.resolve(WorkerService)
        assert workers.has("reports.daily") is True
        assert workers.list() == ["reports.daily"]


@Injectable(scope="request")
class ScopedReportTasks:
    created = 0

    def __init__(self):
        type(self).created += 1
        self.instance_id = type(self).created

    @TaskHandler("reports.scoped")
    async def scoped(self, payload):
        return {"id": self.instance_id, "name": payload["name"]}


@Controller("scoped-workers")
class ScopedWorkerController:
    def __init__(self, workers: WorkerService):
        self.workers = workers

    @Get("/")
    async def index(self):
        return await self.workers.run("reports.scoped", {"name": "inventory"})


@Module(
    imports=[WorkerModule.for_root()],
    controllers=[ScopedWorkerController],
    providers=[ScopedReportTasks],
)
class ScopedWorkerModule:
    pass


def test_request_scoped_worker_tasks_resolve_per_run_scope():
    ScopedReportTasks.created = 0

    with TestClient(FaNestFactory.create(ScopedWorkerModule)) as client:
        assert client.get("/scoped-workers").json() == {"id": 1, "name": "inventory"}
        assert client.get("/scoped-workers").json() == {"id": 2, "name": "inventory"}


@pytest.mark.anyio
async def test_worker_service_missing_task_raises_framework_error():
    workers = WorkerService()

    with pytest.raises(WorkerTaskNotFoundError) as exc:
        await workers.run("missing")

    assert exc.value.name == "missing"


@pytest.mark.anyio
async def test_worker_service_limits_concurrent_batch_runs():
    active = 0
    peak = 0

    async def handler(payload):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return payload

    workers = WorkerService({"concurrency": 2})
    workers.register("limited", handler)

    results = await workers.run_many(
        [("limited", index) for index in range(5)],
        concurrent=True,
    )

    assert results == [0, 1, 2, 3, 4]
    assert peak == 2
    assert workers.stats().completed == 5


@pytest.mark.anyio
async def test_worker_service_cancels_background_runs_on_shutdown():
    started = asyncio.Event()

    async def handler(payload):
        started.set()
        await asyncio.sleep(60)

    workers = WorkerService()
    workers.register("long", handler)
    task = workers.run_background("long")
    await started.wait()

    await asyncio.wait_for(workers.shutdown(), timeout=0.25)

    assert task.cancelled()
    assert workers.active_count() == 0
