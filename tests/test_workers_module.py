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
