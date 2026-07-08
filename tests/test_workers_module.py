from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module
from fanest.workers import TaskHandler, WorkerModule, WorkerService


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
    with TestClient(FaNestFactory.create(WorkerAppModule)) as client:
        assert client.get("/workers").json() == {"report": "sales"}


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
