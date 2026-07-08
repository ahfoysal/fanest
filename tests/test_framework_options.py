from fastapi.testclient import TestClient

from fanest import (
    Controller,
    FaNestFactory,
    Get,
    Module,
    Param,
    ParseIntPipe,
    Req,
    UsePipes,
)
from fanest.common.exceptions import NotFoundException


@Controller("items")
class ItemsController:
    @UsePipes(ParseIntPipe())
    @Get("/{item_id}")
    async def find_one(self, item_id=Param()):
        return {"id": item_id, "type": type(item_id).__name__}

    @Get("/request-info")
    async def request_info(self, req=Req()):
        return {"method": req.method}

    @Get("/missing")
    async def missing(self):
        raise NotFoundException("Item not found")


@Module(controllers=[ItemsController])
class ItemsModule:
    pass


def test_global_prefix_and_request_binding():
    client = TestClient(FaNestFactory.create(ItemsModule, global_prefix="api"))

    assert client.get("/api/items/request-info").json() == {"method": "GET"}


def test_builtin_pipe_and_http_exception():
    client = TestClient(FaNestFactory.create(ItemsModule))

    assert client.get("/items/42").json() == {"id": 42, "type": "int"}
    response = client.get("/items/not-an-int")
    assert response.status_code == 400

    missing = client.get("/items/missing")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Item not found"}


def test_cors_middleware_can_be_enabled():
    client = TestClient(FaNestFactory.create(ItemsModule, cors=True))

    response = client.options(
        "/items/42",
        headers={
            "origin": "https://example.com",
            "access-control-request-method": "GET",
        },
    )

    assert "access-control-allow-origin" not in response.headers


def test_cors_middleware_allows_explicit_permissive_options():
    client = TestClient(
        FaNestFactory.create(
            ItemsModule,
            cors={"allow_origins": ["*"], "allow_methods": ["*"], "allow_headers": ["*"]},
        )
    )

    response = client.options(
        "/items/42",
        headers={
            "origin": "https://example.com",
            "access-control-request-method": "GET",
        },
    )

    assert response.headers["access-control-allow-origin"] == "*"
