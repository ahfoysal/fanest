from fastapi.testclient import TestClient
import asyncio
import os
import pytest

from fanest import Controller, FaNestFactory, Get, Injectable, Module, Post
from fanest.mailer import MailAttachment, MailMessage, MailerModule, MailerService, SmtpMailerTransport
from fanest.queues import Job, MemoryQueueBackend, Process, Processor, QueueModule, QueueService, RedisStreamQueueBackend


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


class FakeRedisStreamClient:
    def __init__(self):
        self.streams: dict[str, list[tuple[bytes, dict[bytes, bytes]]]] = {}
        self.deleted: list[str] = []
        self._sequence = 0

    def xadd(self, stream, fields):
        self._sequence += 1
        message_id = f"{self._sequence}-0".encode()
        encoded = {
            self._encode(key): self._encode(value)
            for key, value in fields.items()
        }
        self.streams.setdefault(stream, []).append((message_id, encoded))
        return message_id

    def xrange(self, stream):
        return list(self.streams.get(stream, []))

    def scan_iter(self, match):
        prefix = match[:-1] if match.endswith("*") else match
        return [key.encode() for key in self.streams if key.startswith(prefix)]

    def delete(self, *streams):
        for stream in streams:
            decoded = stream.decode() if isinstance(stream, bytes) else stream
            self.deleted.append(decoded)
            self.streams.pop(decoded, None)
        return len(streams)

    def _encode(self, value):
        if isinstance(value, bytes):
            return value
        return str(value).encode()


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


@pytest.mark.anyio
async def test_redis_stream_queue_backend_round_trips_jobs_and_clears_prefix():
    client = FakeRedisStreamClient()
    backend = RedisStreamQueueBackend(prefix="fanest:test-queue:", client=client)
    job = Job(
        id="job-1",
        queue="emails",
        name="welcome",
        data={"email": "ada@example.com"},
        max_attempts=3,
        delay=1.5,
        metadata={"backoff": {"type": "fixed", "delay": 0.1}},
    )
    client.streams["other:queue"] = [(b"1-0", {})]

    await backend.add(job)

    restored = backend.jobs("emails")
    assert len(restored) == 1
    assert restored[0].id == "job-1"
    assert restored[0].queue == "emails"
    assert restored[0].name == "welcome"
    assert restored[0].data == {"email": "ada@example.com"}
    assert restored[0].max_attempts == 3
    assert restored[0].delay == 1.5
    assert restored[0].metadata == {"backoff": {"type": "fixed", "delay": 0.1}}

    backend.clear()

    assert backend.jobs("emails") == []
    assert "other:queue" in client.streams
    assert client.deleted == ["fanest:test-queue:emails"]


def test_queue_module_passes_redis_client_hook_to_service():
    client = FakeRedisStreamClient()

    @Module(imports=[QueueModule.for_root(redis_client=client, redis_prefix="fanest:test-queue:")])
    class RedisQueueAppModule:
        pass

    app = FaNestFactory.create(RedisQueueAppModule)
    queue = app.state.fanest_container.resolve(QueueService)

    async def add_job():
        return await queue.add("emails", {"email": "ada@example.com"}, name="missing")

    job = asyncio.run(add_job())

    assert client.streams["fanest:test-queue:emails"][0][1][b"id"] == job.id.encode()


@pytest.mark.live_redis
@pytest.mark.skipif(
    not os.getenv("FANEST_LIVE_REDIS_URL"),
    reason="set FANEST_LIVE_REDIS_URL to run live Redis checks",
)
@pytest.mark.anyio
async def test_live_redis_queue_backend_when_enabled():
    backend = RedisStreamQueueBackend(
        url=os.environ["FANEST_LIVE_REDIS_URL"],
        prefix="fanest:live-queue:",
    )
    try:
        backend.clear()
        job = await backend.add(
            Job(
                id="live-job-1",
                queue="emails",
                name="welcome",
                data={"email": "live@example.com"},
            )
        )
        assert backend.jobs("emails")[0].id == job.id
    finally:
        backend.clear()


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


@pytest.mark.anyio
async def test_delayed_queue_jobs_are_visible_and_cancel_on_close():
    class DelayedProcessor:
        handled: list[str] = []

        async def handle(self, job):
            type(self).handled.append(job.data["id"])

    processor = DelayedProcessor()
    queue = QueueService()
    queue.register_processor("delayed", "run", processor.handle)

    job = await queue.add("delayed", {"id": "slow"}, name="run", delay=60)

    assert queue.delayed_jobs("delayed")[0].id == job.id
    assert queue.stats("delayed").delayed == 1

    await queue.close()

    assert DelayedProcessor.handled == []
    assert queue.delayed_jobs("delayed") == []
    assert queue.stats("delayed").delayed == 0


@pytest.mark.anyio
async def test_queue_stats_track_failed_dead_letter_and_retry():
    attempts: list[int] = []

    async def always_fails(job):
        attempts.append(job.attempts)
        raise RuntimeError("boom")

    queue = QueueService()
    queue.register_processor("reports", "generate", always_fails)

    await queue.add("reports", {}, name="generate", attempts=2)

    stats = queue.stats("reports")
    assert attempts == [1, 2]
    assert stats.waiting == 0
    assert stats.failed == 1
    assert stats.dead_letter == 1
    assert queue.failed_jobs("reports")[0].failed_reason == "boom"
    assert [item.success for item in queue.attempts(queue.failed_jobs("reports")[0].id)] == [False, False]


@pytest.mark.anyio
async def test_queue_introspection_get_job_waiting_jobs_and_clean():
    queue = QueueService()
    completed_job = await queue.add("reports", {"id": "ok"}, name="missing-handler")

    assert queue.waiting_jobs("reports") == [completed_job]
    assert queue.stats("reports").waiting == 1
    assert queue.get_job(completed_job.id).data == {"id": "ok"}

    async def succeeds(job):
        return None

    queue.register_processor("reports", "handled", succeeds)
    handled_job = await queue.add("reports", {"id": "handled"}, name="handled")

    assert queue.stats("reports").completed == 1
    assert queue.get_job(handled_job.id).status == "completed"
    assert queue.attempts(handled_job.id)[0].success is True
    assert queue.clean(status="completed", queue="reports") == 1
    assert queue.completed_jobs("reports") == []


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


class RecordingMailerTransport:
    def __init__(self):
        self.messages: list[MailMessage] = []

    def send(self, message: MailMessage) -> None:
        self.messages.append(message)


class AsyncRecordingMailerTransport:
    def __init__(self):
        self.messages: list[MailMessage] = []

    async def send(self, message: MailMessage) -> None:
        self.messages.append(message)


RECORDING_MAILER_TRANSPORT = RecordingMailerTransport()
ASYNC_RECORDING_MAILER_TRANSPORT = AsyncRecordingMailerTransport()


@Module(
    imports=[
        MailerModule.for_root(
            default_from="noreply@example.com",
            transport=RECORDING_MAILER_TRANSPORT,
        )
    ]
)
class TransportMailerModule:
    pass


@Module(imports=[MailerModule.for_root(transport=ASYNC_RECORDING_MAILER_TRANSPORT)])
class AsyncTransportMailerModule:
    pass


def test_mailer_module_sends_to_outbox():
    client = TestClient(FaNestFactory.create(MailAppModule))

    assert client.post("/mail").json() == {
        "to": "ada@example.com",
        "text": "Hello Ada",
        "count": 1,
    }
    assert client.get("/mail").json() == {"count": 1}


def test_mailer_module_supports_custom_transport_and_rich_message_fields(tmp_path):
    attachment = tmp_path / "report.txt"
    attachment.write_text("hello", encoding="utf-8")
    RECORDING_MAILER_TRANSPORT.messages = []
    app = FaNestFactory.create(TransportMailerModule)
    mailer = app.state.fanest_container.resolve(MailerService)

    message = mailer.send(
        to=["ada@example.com"],
        cc="grace@example.com",
        bcc="ops@example.com",
        reply_to="support@example.com",
        subject="Report",
        text="Attached",
        attachments=[attachment],
    )

    assert RECORDING_MAILER_TRANSPORT.messages == [message]
    assert message.sender == "noreply@example.com"
    assert message.cc == "grace@example.com"
    assert message.bcc == "ops@example.com"
    assert message.reply_to == "support@example.com"
    assert message.attachments == [attachment]


def test_mailer_module_supports_async_transports_and_attachment_objects():
    ASYNC_RECORDING_MAILER_TRANSPORT.messages = []
    app = FaNestFactory.create(AsyncTransportMailerModule)
    mailer = app.state.fanest_container.resolve(MailerService)

    async def send_mail():
        return await mailer.send_async(
            to="ada@example.com",
            subject="Async",
            text="hello",
            attachments=[
                MailAttachment(
                    filename="report.txt",
                    content=b"hello",
                    content_type="text/plain",
                )
            ],
        )

    message = asyncio.run(send_mail())

    assert ASYNC_RECORDING_MAILER_TRANSPORT.messages == [message]
    assert message.attachments is not None
    assert message.attachments[0] == MailAttachment(
        filename="report.txt",
        content=b"hello",
        content_type="text/plain",
    )


def test_smtp_transport_builds_mime_message_without_leaking_bcc():
    transport = SmtpMailerTransport({"from": "noreply@example.com"})
    message = MailMessage(
        to=["ada@example.com"],
        cc="grace@example.com",
        bcc="ops@example.com",
        reply_to="support@example.com",
        subject="Report",
        text="Plain",
        html="<strong>Plain</strong>",
        attachments=[
            MailAttachment(
                filename="report.txt",
                content=b"hello",
                content_type="text/plain",
            )
        ],
    )

    email = transport.build_email(message)

    assert email["To"] == "ada@example.com"
    assert email["Cc"] == "grace@example.com"
    assert email["Reply-To"] == "support@example.com"
    assert "Bcc" not in email
    assert email.is_multipart()
