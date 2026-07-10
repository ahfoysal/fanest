from fastapi.testclient import TestClient

from fanest import (
    Controller,
    FaNestFactory,
    Get,
    Module,
    Param,
    Post,
    ParseIntPipe,
    Req,
    UsePipes,
    Version,
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

    @Post("/raw")
    async def raw(self, req=Req()):
        parsed = await req.json()
        return {
            "raw": req.raw_body.decode(),
            "state_raw": req.state.raw_body.decode(),
            "parsed": parsed,
        }

    @Version("2")
    @Post("/versioned-raw")
    async def versioned_raw(self, req=Req()):
        return {"raw": req.raw_body.decode(), "path": req.url.path}


@Module(controllers=[ItemsController])
class ItemsModule:
    pass


def test_global_prefix_and_request_binding():
    client = TestClient(FaNestFactory.create(ItemsModule, global_prefix="api"))

    assert client.get("/api/items/request-info").json() == {"method": "GET"}


def test_global_prefix_rejects_ambiguous_segments():
    try:
        FaNestFactory.create(ItemsModule, global_prefix="/api/../internal")
    except ValueError as exc:
        assert "global_prefix" in str(exc)
    else:
        raise AssertionError("invalid global_prefix should fail")


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


def test_cors_middleware_supports_regex_exposed_headers_and_max_age():
    client = TestClient(
        FaNestFactory.create(
            ItemsModule,
            cors={
                "allow_origin_regex": r"https://.*\.example\.com",
                "allow_methods": ["GET"],
                "allow_headers": ["authorization"],
                "expose_headers": ["x-request-id"],
                "max_age": 120,
            },
        )
    )

    response = client.options(
        "/items/42",
        headers={
            "origin": "https://api.example.com",
            "access-control-request-method": "GET",
            "access-control-request-headers": "authorization",
        },
    )

    assert response.headers["access-control-allow-origin"] == "https://api.example.com"
    assert response.headers["access-control-max-age"] == "120"


def test_raw_body_option_preserves_request_body_for_handlers():
    client = TestClient(FaNestFactory.create(ItemsModule, raw_body=True))

    response = client.post("/items/raw", json={"ok": True})

    assert response.json() == {
        "raw": '{"ok":true}',
        "state_raw": '{"ok":true}',
        "parsed": {"ok": True},
    }


def test_global_prefix_uri_versioning_and_raw_body_share_one_route_shape():
    client = TestClient(
        FaNestFactory.create(
            ItemsModule,
            global_prefix="/api",
            raw_body=True,
            versioning=True,
        )
    )

    response = client.post("/api/v2/items/versioned-raw", json={"ok": True})

    assert response.status_code == 201
    assert response.json() == {
        "raw": '{"ok":true}',
        "path": "/api/v2/items/versioned-raw",
    }
    assert client.post("/api/v3/items/versioned-raw", json={"ok": True}).status_code == 404


def test_application_wrapper_exposes_fastapi_adapter_and_server_options(monkeypatch):
    captured = {}

    def fake_run(app, **options):
        captured["app"] = app
        captured["options"] = options

    monkeypatch.setattr("uvicorn.run", fake_run)

    app = FaNestFactory.create_application(
        ItemsModule,
        global_prefix="api",
        cors={"allow_origins": ["https://app.test"]},
        raw_body=True,
    )

    assert app.get_http_adapter().__class__.__name__ == "FastApiAdapter"
    assert app.serverless_handler() is app.fastapi

    app.listen(
        host="0.0.0.0",
        port=8443,
        ssl_keyfile="key.pem",
        ssl_certfile="cert.pem",
        timeout_keep_alive=30,
        workers=2,
    )

    assert captured["app"] is app.fastapi
    assert captured["options"]["ssl_keyfile"] == "key.pem"
    assert captured["options"]["ssl_certfile"] == "cert.pem"
    assert captured["options"]["timeout_keep_alive"] == 30
    assert captured["options"]["workers"] == 2


def test_application_wrapper_rejects_multiple_server_specs():
    app = FaNestFactory.create_application(ItemsModule)

    try:
        app.listen(servers=[{"host": "127.0.0.1", "port": 8000}])
    except NotImplementedError as exc:
        assert "one HTTP server" in str(exc)
    else:
        raise AssertionError("multiple server specs should fail explicitly")
