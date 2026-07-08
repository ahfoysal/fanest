from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Res, SetHeader, Sse, StreamableFile


@Controller("responses")
class ResponseController:
    @SetHeader("x-powered-by", "fanest")
    @Get("/header")
    async def header(self):
        return {"ok": True}

    @SetHeader("x-stream", "yes")
    @Get("/stream")
    async def stream(self):
        return StreamableFile(b"hello", filename="hello.txt", content_type="text/plain")

    @SetHeader("x-sse", "yes")
    @Sse("/events")
    async def events(self):
        yield {"event": "message", "data": {"text": "hello"}}

    @Get("/manual")
    async def manual(self, response=Res()):
        response.status_code = 204
        response.headers["x-manual"] = "yes"

    @Get("/passthrough")
    async def passthrough(self, response=Res(passthrough=True)):
        response.headers["x-pass"] = "yes"
        return {"ok": True}


@Module(controllers=[ResponseController])
class ResponseModule:
    pass


def test_response_header_decorator_sets_headers():
    client = TestClient(FaNestFactory.create(ResponseModule))

    response = client.get("/responses/header")

    assert response.json() == {"ok": True}
    assert response.headers["x-powered-by"] == "fanest"


def test_streamable_file_returns_streaming_response_with_headers():
    client = TestClient(FaNestFactory.create(ResponseModule))

    response = client.get("/responses/stream")

    assert response.text == "hello"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["content-disposition"] == 'attachment; filename="hello.txt"'
    assert response.headers["x-stream"] == "yes"


def test_sse_decorator_formats_event_streams():
    client = TestClient(FaNestFactory.create(ResponseModule))

    response = client.get("/responses/events")

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-sse"] == "yes"
    assert 'event: message\ndata: {"text": "hello"}' in response.text


def test_response_decorator_supports_manual_and_passthrough_modes():
    client = TestClient(FaNestFactory.create(ResponseModule))

    manual = client.get("/responses/manual")
    passthrough = client.get("/responses/passthrough")

    assert manual.status_code == 204
    assert manual.text == ""
    assert manual.headers["x-manual"] == "yes"
    assert passthrough.json() == {"ok": True}
    assert passthrough.headers["x-pass"] == "yes"
