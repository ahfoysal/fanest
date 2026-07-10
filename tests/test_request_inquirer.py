import asyncio

import httpx
from fastapi import Request
from fastapi.testclient import TestClient

from fanest import (
    INQUIRER,
    REQUEST,
    Controller,
    FaNestFactory,
    Get,
    Inject,
    Injectable,
    Module,
)


@Injectable(scope="request")
class RequestHolder:
    def __init__(self, request=Inject(REQUEST)):
        self.request = request


@Controller("request-token")
class RequestTokenController:
    def __init__(self, holder: RequestHolder, request=Inject(REQUEST)):
        self.holder = holder
        self.request = request

    @Get("/")
    async def index(self):
        return {
            "path": self.request.url.path,
            "same_as_holder": self.holder.request is self.request,
            "is_request": isinstance(self.request, Request),
            "marker": self.request.headers.get("x-marker"),
        }


@Module(controllers=[RequestTokenController], providers=[RequestHolder])
class RequestTokenModule:
    pass


def test_request_token_injects_current_http_request():
    client = TestClient(FaNestFactory.create(RequestTokenModule))

    body = client.get("/request-token", headers={"x-marker": "abc"}).json()

    assert body["path"] == "/request-token"
    assert body["same_as_holder"] is True
    assert body["is_request"] is True
    assert body["marker"] == "abc"


@Injectable()
class SingletonRequestReader:
    """No explicit scope: injecting REQUEST must bubble it to request scope."""

    instances = 0

    def __init__(self, request=Inject(REQUEST)):
        type(self).instances += 1
        self.request = request


@Controller("bubbled")
class BubbledController:
    def __init__(self, reader: SingletonRequestReader):
        self.reader = reader

    @Get("/")
    async def index(self):
        await asyncio.sleep(0.02)
        return {
            "marker": self.reader.request.headers.get("x-marker"),
            "instances": SingletonRequestReader.instances,
        }


@Module(controllers=[BubbledController], providers=[SingletonRequestReader])
class BubbledModule:
    pass


def test_request_token_bubbles_scope_and_never_bleeds_across_concurrent_requests():
    SingletonRequestReader.instances = 0
    app = FaNestFactory.create(BubbledModule)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await asyncio.gather(
                *[client.get("/bubbled", headers={"x-marker": f"req-{i}"}) for i in range(20)]
            )

    responses = asyncio.run(run())

    markers = sorted(response.json()["marker"] for response in responses)
    assert markers == sorted(f"req-{i}" for i in range(20))
    # A fresh consumer instance per request proves the scope bubbled up.
    assert SingletonRequestReader.instances == 20


@Injectable(scope="transient")
class InquirerLogger:
    def __init__(self, parent=Inject(INQUIRER)):
        self.parent = parent


@Injectable()
class CatsService:
    def __init__(self, logger: InquirerLogger):
        self.logger = logger


@Injectable()
class DogsService:
    def __init__(self, logger: InquirerLogger):
        self.logger = logger


@Controller("inquirer")
class InquirerController:
    def __init__(self, cats: CatsService, dogs: DogsService, logger: InquirerLogger):
        self.cats = cats
        self.dogs = dogs
        self.logger = logger

    @Get("/")
    async def index(self):
        return {
            "cats_parent": self.cats.logger.parent.__name__,
            "dogs_parent": self.dogs.logger.parent.__name__,
            "controller_parent": self.logger.parent.__name__,
        }


@Module(
    controllers=[InquirerController],
    providers=[InquirerLogger, CatsService, DogsService],
)
class InquirerModule:
    pass


def test_inquirer_token_resolves_to_the_consuming_class():
    client = TestClient(FaNestFactory.create(InquirerModule))

    body = client.get("/inquirer").json()

    assert body["cats_parent"] == "CatsService"
    assert body["dogs_parent"] == "DogsService"
    assert body["controller_parent"] == "InquirerController"


def test_request_token_outside_http_context_resolves_to_none():
    app = FaNestFactory.create(RequestTokenModule)
    container = app.state.fanest_container

    assert container.resolve(REQUEST) is None


def test_inquirer_at_top_of_resolution_chain_is_none():
    app = FaNestFactory.create(InquirerModule)
    container = app.state.fanest_container

    assert container.resolve(INQUIRER) is None
