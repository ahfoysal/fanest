from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module


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
