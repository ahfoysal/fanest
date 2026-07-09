from fastapi.testclient import TestClient
import asyncio

from fanest import Controller, FaNestFactory, Get, Injectable, Module, Post
from fanest.mailer import MailerModule, MailerService
from fanest.queues import Job, MemoryQueueBackend, Process, Processor, QueueModule, QueueService


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


class RecordingQueueBackend(MemoryQueueBackend):
    added: list[str]

    def __init__(self):
        super().__init__()
        self.added = []

    async def add(self, job: Job) -> Job:
        self.added.append(job.id)
        return await super().add(job)


RECORDING_QUEUE_BACKEND = RecordingQueueBackend()


@Module(
    imports=[QueueModule.for_root(backend=RECORDING_QUEUE_BACKEND)],
    controllers=[QueueController],
    providers=[EmailProcessor],
)
class QueueBackendAppModule:
    pass


def test_queue_module_registers_processors_and_runs_jobs():
    EmailProcessor.handled = []

    with TestClient(FaNestFactory.create(QueueAppModule)) as client:
        response = client.post("/queues")

    assert response.status_code == 200
    assert response.json()["handled"] == ["ada@example.com"]


def test_queue_module_accepts_custom_backend_for_durable_adapters():
    EmailProcessor.handled = []
    RECORDING_QUEUE_BACKEND.clear()
    RECORDING_QUEUE_BACKEND.added = []

    with TestClient(FaNestFactory.create(QueueBackendAppModule)) as client:
        response = client.post("/queues")

    assert response.status_code == 200
    assert response.json()["handled"] == ["ada@example.com"]
    assert len(RECORDING_QUEUE_BACKEND.added) == 1
    assert [job.queue for job in RECORDING_QUEUE_BACKEND.jobs()] == ["emails"]


def test_queue_processors_are_not_duplicated_across_repeated_lifespan_startups():
    EmailProcessor.handled = []
    app = FaNestFactory.create(QueueAppModule)

    with TestClient(app) as client:
        assert client.post("/queues").status_code == 200
    with TestClient(app) as client:
        assert client.post("/queues").status_code == 200

    assert EmailProcessor.handled == ["ada@example.com", "ada@example.com"]


@Injectable(scope="request")
@Processor("scoped")
class ScopedProcessor:
    created = 0
    handled: list[int] = []

    def __init__(self):
        type(self).created += 1
        self.instance_id = type(self).created

    @Process("run")
    async def run(self, job):
        type(self).handled.append(self.instance_id)


@Controller("scoped-queues")
class ScopedQueueController:
    def __init__(self, queue: QueueService):
        self.queue = queue

    @Post("/")
    async def enqueue(self):
        await self.queue.add("scoped", {}, name="run")
        return {"handled": ScopedProcessor.handled}


@Module(
    imports=[QueueModule.for_root()],
    controllers=[ScopedQueueController],
    providers=[ScopedProcessor],
)
class ScopedQueueModule:
    pass


def test_request_scoped_queue_processors_resolve_per_job_scope():
    ScopedProcessor.created = 0
    ScopedProcessor.handled = []

    with TestClient(FaNestFactory.create(ScopedQueueModule)) as client:
        assert client.post("/scoped-queues").json() == {"handled": [1]}
        assert client.post("/scoped-queues").json() == {"handled": [1, 2]}

    assert ScopedProcessor.created == 2


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
