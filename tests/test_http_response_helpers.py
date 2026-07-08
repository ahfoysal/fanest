from fastapi import BackgroundTasks, Request, Response
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module, Res, SetHeader, Sse, StreamableFile


@Injectable(scope="request")
class StreamingRequestState:
    created = 0

    def __init__(self):
        type(self).created += 1
        self.instance_id = type(self).created


@Controller("responses")
class ResponseController:
    def __init__(self, state: StreamingRequestState):
        self.state = state

    @SetHeader("x-powered-by", "fanest")
    @Get("/header")
    async def header(self):
        return {"ok": True}

    @SetHeader("set-cookie", "session=abc; Path=/")
    @SetHeader("set-cookie", "theme=dark; Path=/")
    @Get("/duplicate-headers")
    async def duplicate_headers(self):
        return {"ok": True}

    @SetHeader("x-stream", "yes")
    @Get("/stream")
    async def stream(self):
        return StreamableFile(b"hello", filename="hello.txt", content_type="text/plain")

    @SetHeader("x-sse", "yes")
    @Sse("/events")
    async def events(self, request: Request):
        container = request.app.state.fanest_container
        resolved_inside_stream = container.resolve(StreamingRequestState)
        yield {
            "event": "message",
            "data": {
                "text": "hello",
                "same_scope": resolved_inside_stream is self.state,
            },
        }

    @Get("/native-params")
    async def native_params(
        self,
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
    ):
        response.headers["x-native"] = "yes"
        return {
            "path": request.url.path,
            "background_tasks": background_tasks is not None,
        }

    @Get("/manual")
    async def manual(self, response=Res()):
        response.status_code = 204
        response.headers["x-manual"] = "yes"

    @Get("/passthrough")
    async def passthrough(self, response=Res(passthrough=True)):
        response.headers["x-pass"] = "yes"
        return {"ok": True}


@Module(controllers=[ResponseController], providers=[StreamingRequestState])
class ResponseModule:
    pass


def test_response_header_decorator_sets_headers():
    client = TestClient(FaNestFactory.create(ResponseModule))

    response = client.get("/responses/header")

    assert response.json() == {"ok": True}
    assert response.headers["x-powered-by"] == "fanest"


def test_response_header_decorator_preserves_duplicate_header_names():
    client = TestClient(FaNestFactory.create(ResponseModule))

    response = client.get("/responses/duplicate-headers")

    assert response.json() == {"ok": True}
    assert response.headers.get_list("set-cookie") == [
        "theme=dark; Path=/",
        "session=abc; Path=/",
    ]


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
    assert 'event: message\ndata: {"text": "hello", "same_scope": true}' in response.text


def test_native_framework_parameter_names_do_not_duplicate_generated_signature():
    client = TestClient(FaNestFactory.create(ResponseModule))

    response = client.get("/responses/native-params")

    assert response.json() == {
        "path": "/responses/native-params",
        "background_tasks": True,
    }
    assert response.headers["x-native"] == "yes"


def test_response_decorator_supports_manual_and_passthrough_modes():
    client = TestClient(FaNestFactory.create(ResponseModule))

    manual = client.get("/responses/manual")
    passthrough = client.get("/responses/passthrough")

    assert manual.status_code == 204
    assert manual.text == ""
    assert manual.headers["x-manual"] == "yes"
    assert passthrough.json() == {"ok": True}
    assert passthrough.headers["x-pass"] == "yes"
