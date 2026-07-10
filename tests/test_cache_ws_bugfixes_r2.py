"""Round-2 regressions for cache TTL semantics and websocket namespace/rooms."""

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, use_class
from fanest.cache import CacheInterceptor, CacheKey, CacheModule, CacheTTL
from fanest.core.enhancers import APP_INTERCEPTOR
from fanest.websockets import WebSocketManager


def test_controller_level_cache_ttl_and_key_apply():
    calls = {"n": 0}

    @CacheKey("things")
    @CacheTTL(60)
    @Controller("c")
    class C:
        @Get("/")
        async def read(self):
            calls["n"] += 1
            return {"n": calls["n"]}

    @Module(
        imports=[CacheModule.for_root(ttl=1)],
        controllers=[C],
        providers=[use_class(APP_INTERCEPTOR, CacheInterceptor)],
    )
    class M:
        pass

    client = TestClient(FaNestFactory.create(M))
    first = client.get("/c").json()
    second = client.get("/c").json()
    assert first == second == {"n": 1}


class _FakeWS:
    def __init__(self, name):
        self.name = name


def test_leave_namespace_also_leaves_its_rooms():
    manager = WebSocketManager()
    alice, bob = _FakeWS("alice"), _FakeWS("bob")
    for socket in (alice, bob):
        manager.connect(socket)
        manager.join("chat", socket, namespace="/app")

    assert set(manager.connections("chat", namespace="/app")) == {alice, bob}
    manager.leave_namespace("/app", alice)
    # Room broadcasts within the namespace must no longer reach the departed socket.
    assert set(manager.connections("chat", namespace="/app")) == {bob}
    assert alice not in manager.connections(namespace="/app")
