import asyncio

import pytest

from fanest import Controller, FaNestFactory, Get, Injectable, Module
from fanest.events import EventEmitter, EventEmitterModule, EventEmitterOptions, EventError, OnEvent


@pytest.mark.anyio
async def test_event_emitter_isolates_sync_and_async_listener_errors():
    captured: list[EventError] = []
    emitter = EventEmitter(EventEmitterOptions(error_handler=captured.append))
    seen: list[str] = []

    def broken_sync(payload):
        raise ValueError(payload)

    async def broken_async(payload):
        await asyncio.sleep(0)
        raise RuntimeError(payload)

    emitter.on("ready", broken_sync)
    emitter.on("ready", broken_async)
    emitter.on("ready", lambda payload: seen.append(payload))

    await emitter.emit("ready", "boom")

    assert seen == ["boom"]
    assert [type(item.error) for item in captured] == [ValueError, RuntimeError]
    assert [type(item.error) for item in emitter.errors()] == [ValueError, RuntimeError]


@pytest.mark.anyio
async def test_event_emitter_strict_mode_reraises_captured_errors():
    emitter = EventEmitter()

    def broken(payload):
        raise ValueError(payload)

    emitter.on("ready", broken)

    with pytest.raises(ValueError, match="boom"):
        await emitter.emit_strict("ready", "boom")


@pytest.mark.anyio
async def test_event_emitter_introspection_wildcard_and_max_listener_options():
    emitter = EventEmitter(EventEmitterOptions(wildcard=False, max_listeners=1))
    seen: list[str] = []

    emitter.on("ready", lambda payload: seen.append(payload))
    emitter.on("*", lambda payload: seen.append(f"wild:{payload}"))

    with pytest.raises(RuntimeError, match="Too many listeners"):
        emitter.once("ready", lambda payload: seen.append(payload))

    assert emitter.listener_count("ready") == 1
    assert set(emitter.event_names()) == {"ready", "*"}

    await emitter.emit("ready", "one")
    assert seen == ["one"]

    emitter.remove_all_listeners("ready")
    assert emitter.listeners("ready") == ()


@pytest.mark.anyio
async def test_event_emitter_supports_segment_wildcards_once_and_listener_priority():
    emitter = EventEmitter()
    seen: list[str] = []

    emitter.on("user.*", lambda payload: seen.append(f"wild:{payload}"), priority=5)
    emitter.on("user.created", lambda payload: seen.append(f"normal:{payload}"))
    emitter.on("user.created", lambda payload: seen.append(f"first:{payload}"), prepend=True, priority=10)
    emitter.once("user.*", lambda payload: seen.append(f"once:{payload}"), priority=6)

    await emitter.emit("user.created", "Ada")
    await emitter.emit("user.created", "Grace")

    assert seen == [
        "first:Ada",
        "once:Ada",
        "wild:Ada",
        "normal:Ada",
        "first:Grace",
        "wild:Grace",
        "normal:Grace",
    ]


@Injectable(scope="request")
class ScopedEventListener:
    created = 0
    seen: list[int] = []

    def __init__(self):
        type(self).created += 1
        self.instance_id = type(self).created

    @OnEvent("scoped.created")
    async def handle(self, payload):
        await asyncio.sleep(0)
        self.seen.append(self.instance_id)


@Controller("scoped-events")
class ScopedEventsController:
    def __init__(self, emitter: EventEmitter):
        self.emitter = emitter

    @Get("/")
    async def index(self):
        self.emitter.emit("scoped.created", {})
        return {"ok": True}


@Module(
    imports=[EventEmitterModule.for_root()],
    controllers=[ScopedEventsController],
    providers=[ScopedEventListener],
)
class ScopedEventsModule:
    pass


def test_event_module_preserves_request_scope_for_fire_and_forget_handlers():
    ScopedEventListener.created = 0
    ScopedEventListener.seen = []

    app = FaNestFactory.create(ScopedEventsModule)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/scoped-events").json() == {"ok": True}
        assert client.get("/scoped-events").json() == {"ok": True}
        assert client.get("/scoped-events").status_code == 200

    assert ScopedEventListener.created == 3
    assert ScopedEventListener.seen == [1, 2, 3]
