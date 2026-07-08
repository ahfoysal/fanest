import asyncio

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Post
from fanest.i18n import I18nLang, I18nModule, I18nService
from fanest.queues import BullModule, InjectQueue, QueueRef, QueueService


@Controller("i18n")
class I18nController:
    def __init__(self, i18n: I18nService):
        self.i18n = i18n

    @Get("/")
    async def index(self, lang: str = I18nLang()):
        return {"message": self.i18n.t("hello", lang=lang, args={"name": "Ada"})}


@Module(
    imports=[
        I18nModule.for_root(
            translations={
                "en": {"hello": "Hello {name}"},
                "bn": {"hello": "Nomoskar {name}"},
            }
        )
    ],
    controllers=[I18nController],
)
class I18nAppModule:
    pass


def test_i18n_module_translates_from_accept_language():
    client = TestClient(FaNestFactory.create(I18nAppModule))

    assert client.get("/i18n", headers={"accept-language": "bn"}).json() == {
        "message": "Nomoskar Ada"
    }
    assert client.get("/i18n").json() == {"message": "Hello Ada"}


@Controller("bull")
class BullController:
    def __init__(self, emails: QueueRef = InjectQueue("emails")):
        self.emails = emails

    @Post("/")
    async def enqueue(self):
        job = await self.emails.add({"email": "ada@example.com"}, name="welcome")
        return {"queue": job.queue, "jobs": len(self.emails.jobs())}


@Module(
    imports=[BullModule.for_root(), BullModule.register_queue("emails")],
    controllers=[BullController],
)
class BullAppModule:
    pass


def test_bull_module_alias_injects_named_queue():
    client = TestClient(FaNestFactory.create(BullAppModule))

    assert client.post("/bull").json() == {"queue": "emails", "jobs": 1}


def test_queue_ref_works_directly():
    service = QueueService()
    queue = service.queue("reports")

    async def add_job():
        return await queue.add({"id": 1})

    job = asyncio.run(add_job())

    assert job.queue == "reports"
    assert queue.jobs() == [job]
