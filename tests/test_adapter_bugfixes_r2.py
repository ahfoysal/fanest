"""Round-2 regressions for the FastAPI platform adapter."""

from fastapi.testclient import TestClient

from fanest import (
    Controller,
    FaNestFactory,
    Get,
    HostParam,
    HttpCode,
    Injectable,
    Module,
    Post,
    UploadedFiles,
    UseInterceptors,
)
from fanest.common.upload import FilesInterceptor
from fanest.common.versioning import VersioningOptions, VersioningType
from fanest.swagger import ApiExcludeController


def test_post_defaults_to_201_and_httpcode_overrides():
    @Controller("t")
    class C:
        @Post("/")
        async def create(self):
            return {"created": True}

        @Post("ok")
        @HttpCode(200)
        async def ok(self):
            return {"ok": True}

        @Get("/")
        async def read(self):
            return {"read": True}

    @Module(controllers=[C])
    class M:
        pass

    client = TestClient(FaNestFactory.create(M))
    assert client.post("/t").status_code == 201
    assert client.post("/t/ok").status_code == 200
    assert client.get("/t").status_code == 200


def test_header_versioning_default_version_rejects_unmatched():
    @Controller("v")
    class C:
        @Get("/")
        async def read(self):
            return {"ok": True}

    @Module(controllers=[C])
    class M:
        pass

    app = FaNestFactory.create(
        M,
        versioning=VersioningOptions(type=VersioningType.HEADER, header="X-Version", default_version="1"),
    )
    client = TestClient(app)
    assert client.get("/v", headers={"X-Version": "1"}).status_code == 200
    assert client.get("/v", headers={"X-Version": "2"}).status_code == 404


def test_api_exclude_controller_still_serves_but_is_undocumented():
    @ApiExcludeController()
    @Controller("hidden")
    class C:
        @Get("/")
        async def read(self):
            return {"ok": True}

    @Module(controllers=[C])
    class M:
        pass

    app = FaNestFactory.create(M)
    assert TestClient(app).get("/hidden").status_code == 200
    assert "/hidden" not in app.openapi().get("paths", {})


def test_host_scoped_controller_only_serves_matching_host():
    @Controller("", host="admin.example.com")
    class Admin:
        @Get("/panel")
        async def panel(self):
            return {"ok": True}

    @Controller("", host="{account}.example.com")
    class Tenant:
        @Get("/who")
        async def who(self, account: str = HostParam("account")):
            return {"account": account}

    @Module(controllers=[Admin, Tenant])
    class M:
        pass

    client = TestClient(FaNestFactory.create(M), raise_server_exceptions=False)
    assert client.get("http://admin.example.com/panel").status_code == 200
    assert client.get("http://public.com/panel").status_code == 404
    assert client.get("http://acme.example.com/who").json() == {"account": "acme"}
    assert client.get("http://evil.other.com/who").status_code == 404


def test_upload_route_with_di_class_interceptor_builds():
    @Injectable()
    class Dependency:
        value = 1

    class LoggingInterceptor:
        def __init__(self, dependency: Dependency):
            self.dependency = dependency

        async def intercept(self, context, call_next):
            return await call_next()

    @Controller("uploads")
    class C:
        @UseInterceptors(FilesInterceptor("files", max_count=3), LoggingInterceptor)
        @Post("/")
        async def upload(self, files=UploadedFiles("files")):
            return {"count": len(files or [])}

    @Module(controllers=[C], providers=[Dependency])
    class M:
        pass

    # Previously crashed at build time instantiating the DI interceptor with no args.
    client = TestClient(FaNestFactory.create(M))
    response = client.post("/uploads", files=[("files", ("a.txt", b"x")), ("files", ("b.txt", b"y"))])
    assert response.status_code == 201
    assert response.json() == {"count": 2}
