import asyncio

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Query
from fanest.microservices import Transport
from fanest.platform_fastapi import FastApiAdapter


class UpperPipe:
    def transform(self, value, metadata):
        if metadata["name"] == "name" and isinstance(value, str):
            return value.upper()
        return value


@Controller("hello")
class HelloController:
    @Get("/")
    async def hello(self, name: str = Query(default="world")):
        return {"hello": name}


@Module(controllers=[HelloController])
class HelloModule:
    pass


def test_application_wrapper_configures_global_options():
    app = (
        FaNestFactory.create_application(HelloModule, title="Wrapped")
        .set_global_prefix("api")
        .enable_cors()
        .use_global_pipes(UpperPipe())
        .build()
    )
    response = TestClient(app).get("/api/hello?name=ada")

    assert app.title == "Wrapped"
    assert response.json() == {"hello": "ADA"}


def test_application_wrapper_exposes_documented_http_adapter_surface():
    wrapped = FaNestFactory.create_application(HelloModule, title="Adapter")

    adapter = wrapped.get_http_adapter()

    assert isinstance(adapter, FastApiAdapter)
    assert adapter.app is wrapped.fastapi
    assert adapter.global_prefix == ""
    assert callable(adapter.register_controllers)
    assert callable(adapter.register_gateways)


def test_serverless_handler_is_stable_asgi_app():
    wrapped = FaNestFactory.create_application(HelloModule, global_prefix="api")

    handler = wrapped.serverless_handler()

    assert handler is wrapped.serverless_handler()
    assert handler is wrapped.fastapi
    assert TestClient(handler).get("/api/hello?name=serverless").json() == {
        "hello": "serverless",
    }


def test_application_listen_passes_https_and_keep_alive_options(monkeypatch):
    captured = {}

    def fake_run(app, **options):
        captured["app"] = app
        captured["options"] = options

    monkeypatch.setattr("uvicorn.run", fake_run)

    wrapped = FaNestFactory.create_application(HelloModule)
    wrapped.listen(
        host="0.0.0.0",
        port=9443,
        ssl_keyfile="key.pem",
        ssl_certfile="cert.pem",
        timeout_keep_alive=30,
        workers=2,
    )

    assert captured["app"] is wrapped.fastapi
    assert captured["options"] == {
        "host": "0.0.0.0",
        "port": 9443,
        "reload": False,
        "ssl_keyfile": "key.pem",
        "ssl_certfile": "cert.pem",
        "timeout_keep_alive": 30,
        "workers": 2,
    }


def test_application_listen_rejects_multiple_http_server_specs():
    wrapped = FaNestFactory.create_application(HelloModule)

    try:
        wrapped.listen(servers=[{"host": "127.0.0.1", "port": 8000}])
    except NotImplementedError as exc:
        assert "one HTTP server" in str(exc)
        assert "build()" in str(exc)
    else:
        raise AssertionError("multiple HTTP server specs should fail explicitly")


def test_hybrid_microservices_close_with_application_lifespan():
    app = FaNestFactory.create(HelloModule)
    server = app.connect_microservice({"transport": Transport.MEMORY})

    with TestClient(app):
        asyncio.run(app.start_all_microservices())
        assert server.transport.connected is True

    assert server.transport.connected is False
