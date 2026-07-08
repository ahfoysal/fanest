from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module
from fanest.core.application import FaNestApplication
from fanest.events import EventEmitter, EventEmitterModule, OnEvent
from fanest.http import HttpModule, HttpService
from fanest.logger import Logger, LoggerModule


@Injectable()
class EventsService:
    seen: list[str] = []

    @OnEvent("user.created")
    async def handle_user_created(self, payload):
        self.seen.append(payload["name"])


@Controller("events")
class EventsController:
    def __init__(self, emitter: EventEmitter, logger: Logger):
        self.emitter = emitter
        self.logger = logger.child("EventsController")

    @Get("/")
    async def emit(self):
        self.logger.log("emitting")
        await self.emitter.emit("user.created", {"name": "Ada"})
        return {"ok": True}


@Module(
    imports=[EventEmitterModule.for_root(), LoggerModule.register()],
    controllers=[EventsController],
    providers=[EventsService],
)
class EventsModule:
    pass


def test_event_emitter_and_logger_module():
    EventsService.seen = []
    app = FaNestFactory.create(EventsModule)

    with TestClient(app) as client:
        assert client.get("/events").json() == {"ok": True}

    assert EventsService.seen == ["Ada"]


@Injectable(scope="request")
class RequestScopedEventService:
    created = 0
    seen: list[int] = []

    def __init__(self):
        type(self).created += 1
        self.instance_id = type(self).created

    @OnEvent("request.event")
    async def handle_request_event(self, payload):
        self.seen.append(self.instance_id)


@Controller("request-events")
class RequestEventsController:
    def __init__(self, emitter: EventEmitter):
        self.emitter = emitter

    @Get("/")
    async def emit(self):
        await self.emitter.emit("request.event", {})
        return {"ok": True}


@Module(
    imports=[EventEmitterModule.for_root()],
    controllers=[RequestEventsController],
    providers=[RequestScopedEventService],
)
class RequestScopedEventsModule:
    pass


def test_event_listeners_resolve_inside_request_scope():
    RequestScopedEventService.created = 0
    RequestScopedEventService.seen = []

    with TestClient(FaNestFactory.create(RequestScopedEventsModule)) as client:
        assert client.get("/request-events").json() == {"ok": True}
        assert client.get("/request-events").json() == {"ok": True}

    assert RequestScopedEventService.created == 2
    assert RequestScopedEventService.seen == [1, 2]


def test_event_listeners_are_not_duplicated_across_repeated_lifespan_startups():
    EventsService.seen = []
    app = FaNestFactory.create(EventsModule)

    with TestClient(app) as client:
        assert client.get("/events").json() == {"ok": True}
    with TestClient(app) as client:
        assert client.get("/events").json() == {"ok": True}

    assert EventsService.seen == ["Ada", "Ada"]


@pytest.mark.anyio
async def test_event_emitter_supports_once_off_and_wildcard():
    emitter = EventEmitter()
    seen: list[str] = []

    def handler(payload):
        seen.append(payload)

    emitter.on("*", lambda payload: seen.append(f"wild:{payload}"))
    emitter.once("ready", handler)
    await emitter.emit("ready", "one")
    await emitter.emit("ready", "two")

    emitter.on("remove", handler)
    emitter.off("remove", handler)
    await emitter.emit("remove", "three")

    assert seen == ["wild:one", "one", "wild:two", "wild:three"]


@Module(imports=[HttpModule.register(base_url="https://example.com")])
class HttpClientModule:
    pass


@pytest.mark.anyio
async def test_http_service_is_injectable():
    app = FaNestFactory.create(HttpClientModule)
    service = app.state.fanest_container.resolve(HttpService)

    assert str(service.client.base_url).rstrip("/") == "https://example.com"
    await service.on_application_shutdown()


def test_application_static_and_compression_helpers(tmp_path: Path):
    static_dir = tmp_path / "public"
    static_dir.mkdir()
    (static_dir / "hello.txt").write_text("hello", encoding="utf-8")

    app = FaNestApplication(EventsModule).serve_static("/public", str(static_dir)).enable_compression().build()

    response = TestClient(app).get("/public/hello.txt")

    assert response.text == "hello"
