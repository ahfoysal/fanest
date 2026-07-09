import asyncio
from typing import Any, cast

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Post
from fanest.i18n import CookieResolver, HeaderResolver, I18nLang, I18nModule, I18nService, QueryResolver
from fanest.queues import BullModule, InjectQueue, QueueRef, QueueService


@Controller("i18n")
class I18nController:
    def __init__(self, i18n: I18nService):
        self.i18n = i18n

    @Get("/")
    async def index(self, lang: str = cast(Any, I18nLang())):
        return {
            "message": self.i18n.t("hello", lang=lang, args={"name": "Ada"}),
            "nested": self.i18n.t("errors.required", lang=lang, args={"field": "email"}),
        }


@Module(
    imports=[
        I18nModule.for_root(
            translations={
                "en": {"hello": "Hello {name}", "errors": {"required": "{field} is required"}},
                "bn": {"hello": "Nomoskar {name}", "errors": {"required": "{field} dorkar"}},
            }
        )
    ],
    controllers=[I18nController],
)
class I18nAppModule:
    pass


def test_i18n_module_translates_from_accept_language():
    client = TestClient(FaNestFactory.create(I18nAppModule))

    assert client.get("/i18n", headers={"accept-language": "en;q=0.3,bn;q=0.9"}).json() == {
        "message": "Nomoskar Ada",
        "nested": "email dorkar",
    }
    assert client.get("/i18n", headers={"accept-language": "bn-BD"}).json() == {
        "message": "Nomoskar Ada",
        "nested": "email dorkar",
    }
    assert client.get("/i18n").json() == {
        "message": "Hello Ada",
        "nested": "email is required",
    }


@Controller("i18n-advanced")
class AdvancedI18nController:
    def __init__(self, i18n: I18nService):
        self.i18n = i18n

    @Get("/")
    async def index(self, lang: str = cast(Any, I18nLang())):
        return {
            "lang": lang,
            "message": self.i18n.t("welcome.deep", lang=lang, args={"user": {"name": "Ada"}}),
            "fallback": self.i18n.t("only_en", lang=lang),
        }


async def async_i18n_options():
    return {
        "translations": {
            "en": {"only_en": "Only English", "welcome": {"deep": "Hello {user.name}"}},
            "fr": {"welcome": {"deep": "Salut {user.name}"}},
            "pt": {"welcome": {"deep": "Ola {user.name}"}},
        },
        "fallback_language": "en",
        "fallbacks": {"pt-BR": "pt"},
        "resolvers": [QueryResolver("locale"), HeaderResolver("x-locale"), CookieResolver("locale")],
    }


@Module(
    imports=[I18nModule.for_root_async(use_factory=async_i18n_options)],
    controllers=[AdvancedI18nController],
)
class AdvancedI18nModule:
    pass


def test_i18n_supports_async_registration_resolvers_fallbacks_and_nested_args():
    client = TestClient(FaNestFactory.create(AdvancedI18nModule))

    assert client.get("/i18n-advanced", params={"locale": "fr"}).json() == {
        "lang": "fr",
        "message": "Salut Ada",
        "fallback": "Only English",
    }
    assert client.get("/i18n-advanced", headers={"x-locale": "pt-BR"}).json()["message"] == "Ola Ada"
    assert client.get("/i18n-advanced", cookies={"locale": "fr"}).json()["message"] == "Salut Ada"


@Controller("bull")
class BullController:
    def __init__(self, emails: QueueRef = InjectQueue("emails")):
        self.emails = emails

    @Post("/")
    async def enqueue(self):
        job = await self.emails.add("welcome", {"email": "ada@example.com"}, priority=1)
        return {"queue": job.queue, "name": job.name, "priority": job.metadata["priority"], "jobs": len(self.emails.jobs())}


@Module(
    imports=[BullModule.for_root(), BullModule.register_queue("emails")],
    controllers=[BullController],
)
class BullAppModule:
    pass


def test_bull_module_alias_injects_named_queue():
    client = TestClient(FaNestFactory.create(BullAppModule))

    assert client.post("/bull").json() == {"queue": "emails", "name": "welcome", "priority": 1, "jobs": 1}


def test_queue_ref_works_directly():
    service = QueueService()
    queue = service.queue("reports")

    async def add_job():
        return await queue.add({"id": 1})

    job = asyncio.run(add_job())

    assert job.queue == "reports"
    assert queue.jobs() == [job]
