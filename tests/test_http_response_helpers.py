from fastapi import BackgroundTasks, Request, Response
from fastapi.testclient import TestClient

from fanest import (
    Controller,
    FaNestApplication,
    FaNestFactory,
    Get,
    Injectable,
    Module,
    Query,
    Render,
    Res,
    SetHeader,
    Sse,
    StreamableFile,
)


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

    @Get("/large")
    async def large(self):
        return {"text": "x" * 1000}

    @Render("<h1>{{name}}</h1>")
    @Get("/render")
    async def render(self):
        return {"name": "Ada"}

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

    @Get("/reserved-query-names")
    async def reserved_query_names(
        self,
        request: str = Query(),
        response: str = Query(),
        background_tasks: str = Query(),
    ):
        return {
            "request": request,
            "response": response,
            "background_tasks": background_tasks,
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


def test_streamable_file_from_path_returns_file_response(tmp_path):
    payload = tmp_path / "payload.txt"
    payload.write_text("from disk", encoding="utf-8")

    @Controller("path-stream")
    class PathStreamController:
        @Get("/")
        async def index(self):
            return StreamableFile.from_path(
                payload,
                filename="payload.txt",
                content_type="text/plain",
            )

    @Module(controllers=[PathStreamController])
    class PathStreamModule:
        pass

    response = TestClient(FaNestFactory.create(PathStreamModule)).get("/path-stream")

    assert response.text == "from disk"
    assert response.headers["content-disposition"] == 'attachment; filename="payload.txt"'


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


def test_reserved_framework_parameter_names_can_still_be_user_query_params():
    client = TestClient(FaNestFactory.create(ResponseModule))

    response = client.get(
        "/responses/reserved-query-names",
        params={
            "request": "hello",
            "response": "world",
            "background_tasks": "later",
        },
    )

    assert response.json() == {
        "request": "hello",
        "response": "world",
        "background_tasks": "later",
    }


def test_response_decorator_supports_manual_and_passthrough_modes():
    client = TestClient(FaNestFactory.create(ResponseModule))

    manual = client.get("/responses/manual")
    passthrough = client.get("/responses/passthrough")

    assert manual.status_code == 204
    assert manual.text == ""
    assert manual.headers["x-manual"] == "yes"
    assert passthrough.json() == {"ok": True}
    assert passthrough.headers["x-pass"] == "yes"


def test_compression_and_render_helpers():
    client = TestClient(
        FaNestApplication(ResponseModule).enable_compression(minimum_size=1).build()
    )

    compressed = client.get("/responses/large", headers={"accept-encoding": "gzip"})
    rendered = client.get("/responses/render")

    assert compressed.headers["content-encoding"] == "gzip"
    assert compressed.json() == {"text": "x" * 1000}
    assert rendered.headers["content-type"].startswith("text/html")
    assert rendered.text == "<h1>Ada</h1>"
