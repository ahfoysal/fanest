from fastapi.testclient import TestClient
import asyncio

from fanest import Controller, FaNestFactory, Get, Module, Post
from fanest.mailer import MailerModule, MailerService
from fanest.queues import Process, Processor, QueueModule, QueueService


@Processor("emails")
class EmailProcessor:
    handled: list[str] = []

    @Process("welcome")
    async def welcome(self, job):
        type(self).handled.append(job.data["email"])


@Controller("queues")
class QueueController:
    def __init__(self, queue: QueueService):
        self.queue = queue

    @Post("/")
    async def enqueue(self):
        job = await self.queue.add("emails", {"email": "ada@example.com"}, name="welcome")
        return {"id": job.id, "handled": EmailProcessor.handled}


@Module(
    imports=[QueueModule.for_root()],
    controllers=[QueueController],
    providers=[EmailProcessor],
)
class QueueAppModule:
    pass


def test_queue_module_registers_processors_and_runs_jobs():
    EmailProcessor.handled = []

    with TestClient(FaNestFactory.create(QueueAppModule)) as client:
        response = client.post("/queues")

    assert response.status_code == 200
    assert response.json()["handled"] == ["ada@example.com"]


@Processor("retry")
class RetryProcessor:
    attempts: list[int] = []

    @Process("unstable")
    async def unstable(self, job):
        type(self).attempts.append(job.attempts)
        if job.attempts < 2:
            raise RuntimeError("try again")


@Module(imports=[QueueModule.for_root()], providers=[RetryProcessor])
class RetryQueueModule:
    pass


def test_queue_jobs_support_retry_attempts():
    RetryProcessor.attempts = []
    app = FaNestFactory.create(RetryQueueModule)

    with TestClient(app):
        queue = app.state.fanest_container.resolve(QueueService)

        async def run_job():
            await queue.add("retry", {"ok": True}, name="unstable", attempts=2)

        asyncio.run(run_job())

    assert RetryProcessor.attempts == [1, 2]


@Controller("mail")
class MailController:
    def __init__(self, mailer: MailerService):
        self.mailer = mailer

    @Post("/")
    async def send(self):
        message = self.mailer.send(
            to="ada@example.com",
            subject="Welcome",
            template="welcome",
            context={"name": "Ada"},
        )
        return {"to": message.to, "text": message.text, "count": len(self.mailer.outbox)}

    @Get("/")
    async def index(self):
        return {"count": len(self.mailer.outbox)}


@Module(
    imports=[
        MailerModule.for_root(
            default_from="noreply@example.com",
            templates={"welcome": "Hello {{ name }}"},
        )
    ],
    controllers=[MailController],
)
class MailAppModule:
    pass


def test_mailer_module_sends_to_outbox():
    client = TestClient(FaNestFactory.create(MailAppModule))

    assert client.post("/mail").json() == {
        "to": "ada@example.com",
        "text": "Hello Ada",
        "count": 1,
    }
    assert client.get("/mail").json() == {"count": 1}
