import asyncio

import httpx
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Inject, Injectable, Module, token, use_factory


@Injectable()
class SingletonCounter:
    created = 0

    def __init__(self):
        type(self).created += 1


@Injectable(scope="request")
class RequestCounter:
    created = 0

    def __init__(self):
        type(self).created += 1


@Injectable(scope="transient")
class TransientCounter:
    created = 0

    def __init__(self):
        type(self).created += 1


@Injectable(scope="request")
class ScopeService:
    def __init__(
        self,
        singleton: SingletonCounter,
        request: RequestCounter,
        transient: TransientCounter,
    ):
        self.singleton = singleton
        self.request = request
        self.transient = transient


@Controller("scopes")
class ScopeController:
    def __init__(self, service: ScopeService, request_counter: RequestCounter):
        self.service = service
        self.request_counter = request_counter

    @Get("/")
    async def index(self):
        return {
            "same_request": self.service.request is self.request_counter,
            "singleton_created": SingletonCounter.created,
            "request_created": RequestCounter.created,
            "transient_created": TransientCounter.created,
        }


@Module(
    controllers=[ScopeController],
    providers=[SingletonCounter, RequestCounter, TransientCounter, ScopeService],
)
class ScopeModule:
    pass


def test_singleton_request_and_transient_provider_scopes():
    SingletonCounter.created = 0
    RequestCounter.created = 0
    TransientCounter.created = 0
    client = TestClient(FaNestFactory.create(ScopeModule))

    first = client.get("/scopes").json()
    second = client.get("/scopes").json()

    assert first["same_request"] is True
    assert second["same_request"] is True
    assert second["singleton_created"] == 1
    assert second["request_created"] == 2
    assert second["transient_created"] == 2


@Injectable()
class SingletonWithRequestDependency:
    created = 0

    def __init__(self, request: RequestCounter):
        type(self).created += 1
        self.request = request


@Controller("scope-bubbling")
class ScopeBubblingController:
    def __init__(self, service: SingletonWithRequestDependency, request: RequestCounter):
        self.service = service
        self.request = request

    @Get("/")
    async def index(self):
        return {
            "same_request": self.service.request is self.request,
            "service_created": SingletonWithRequestDependency.created,
            "request_created": RequestCounter.created,
        }


@Module(controllers=[ScopeBubblingController], providers=[RequestCounter, SingletonWithRequestDependency])
class ScopeBubblingModule:
    pass


def test_request_scope_bubbles_to_singletons_that_depend_on_request_providers():
    RequestCounter.created = 0
    SingletonWithRequestDependency.created = 0
    client = TestClient(FaNestFactory.create(ScopeBubblingModule))

    first = client.get("/scope-bubbling").json()
    second = client.get("/scope-bubbling").json()

    assert first["same_request"] is True
    assert second["same_request"] is True
    assert second["service_created"] == 2
    assert second["request_created"] == 2


SLOW_DEPENDENCY = token("SLOW_DEPENDENCY")
ASYNC_FACTORY_TOKEN = token("ASYNC_FACTORY_TOKEN")


async def slow_dependency_factory():
    await asyncio.sleep(0.02)
    return {"ready": True}


async def async_factory_value():
    await asyncio.sleep(0)
    return {"ready": True}


@Injectable(scope="request")
class SlowRequestService:
    def __init__(self, slow=Inject(SLOW_DEPENDENCY)):
        self.slow = slow


@Controller("concurrent-scopes")
class ConcurrentScopeController:
    def __init__(self, service: SlowRequestService):
        self.service = service

    @Get("/")
    async def index(self):
        return {"ready": self.service.slow["ready"]}


@Module(
    controllers=[ConcurrentScopeController],
    providers=[use_factory(SLOW_DEPENDENCY, slow_dependency_factory), SlowRequestService],
)
class ConcurrentScopeModule:
    pass


@Module(providers=[use_factory(ASYNC_FACTORY_TOKEN, async_factory_value)])
class AsyncFactoryModule:
    pass


async def _concurrent_get(client: httpx.AsyncClient):
    return await client.get("/concurrent-scopes")


def test_concurrent_request_scoped_resolution_does_not_false_positive_circular_dependency():
    app = FaNestFactory.create(ConcurrentScopeModule)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await asyncio.gather(*[_concurrent_get(client) for _ in range(12)])

    responses = asyncio.run(run())

    assert [response.status_code for response in responses] == [200] * 12
    assert [response.json() for response in responses] == [{"ready": True}] * 12


def test_sync_resolve_rejects_async_factory_without_caching_coroutine():
    container = FaNestFactory.create(AsyncFactoryModule).state.fanest_container

    try:
        container.resolve(ASYNC_FACTORY_TOKEN)
    except RuntimeError as exc:
        assert "resolve_async" in str(exc)
    else:  # pragma: no cover - explicit failure text
        raise AssertionError("sync resolve should reject async factory providers")

    assert asyncio.run(container.resolve_async(ASYNC_FACTORY_TOKEN)) == {"ready": True}
