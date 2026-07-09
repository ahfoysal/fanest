from fastapi.testclient import TestClient

from fanest import (
    ClassSerializerInterceptor,
    Controller,
    Cookie,
    Exclude,
    Expose,
    FaNestFactory,
    Get,
    Headers,
    HttpCode,
    Module,
    Post,
    Redirect,
    SetHeader,
    SetMetadata,
    Serialize,
    UploadedFile,
    UseInterceptors,
    Version,
    VersioningType,
    create_param_decorator,
)


CurrentTenant = create_param_decorator(
    lambda data, context: context.request.headers.get(data or "x-tenant", "public")
)


AsyncTenant = create_param_decorator(
    lambda data, context: _async_header_value(context, data or "x-tenant", "public")
)


async def _async_header_value(context, name: str, default: str):
    return context.request.headers.get(name, default)


@Controller("deep")
class DeepController:
    @HttpCode(204)
    @Post("empty")
    async def empty(self):
        return None

    @SetHeader("x-redirect", "yes")
    @Redirect("/deep/target", status_code=307)
    @Get("redirect")
    async def redirect(self):
        return None

    @Get("cookie")
    async def cookie(self, session: str = Cookie("session")):
        return {"session": session}

    @SetMetadata("role", "admin")
    @Get("tenant")
    async def tenant(self, tenant: str = CurrentTenant("x-org")):
        return {"tenant": tenant}

    @Get("async-tenant")
    async def async_tenant(self, tenant: str = AsyncTenant("x-org")):
        return {"tenant": tenant}

    @Get("headers")
    async def headers(self, headers=Headers()):
        return {"tenant": headers.get("x-org")}

    @Post("upload")
    async def upload(self, file=UploadedFile()):
        return {"filename": file.filename}

    @Post("optional-upload")
    async def optional_upload(self, file=UploadedFile(default=None)):
        return {"filename": file.filename if file is not None else None}


@Expose("token", groups={"admin"})
@Exclude("password")
class AccountView:
    def __init__(self):
        self.email = "ada@example.com"
        self.password = "hidden"
        self.token = "admin-token"


@UseInterceptors(ClassSerializerInterceptor)
@Controller("serialize-deep")
class SerializationController:
    @Serialize()
    @Get("public")
    async def public(self):
        return AccountView()

    @Serialize(groups={"admin"})
    @Get("admin")
    async def admin(self):
        return AccountView()


@Controller("versioned")
class VersionedController:
    @Version(["1", "2"])
    @Get("uri")
    async def uri(self):
        return {"strategy": "uri"}

    @Version("3")
    @Get("selected")
    async def selected(self):
        return {"strategy": "selected"}


@Module(controllers=[DeepController, SerializationController, VersionedController])
class DeepModule:
    pass


def test_http_metadata_cookie_custom_param_redirect_and_upload():
    client = TestClient(FaNestFactory.create(DeepModule))

    assert client.post("/deep/empty").status_code == 204
    redirect = client.get("/deep/redirect", follow_redirects=False)
    assert redirect.headers["location"] == "/deep/target"
    assert redirect.headers["x-redirect"] == "yes"
    assert client.get("/deep/cookie", cookies={"session": "abc"}).json() == {"session": "abc"}
    assert client.get("/deep/tenant", headers={"x-org": "acme"}).json() == {"tenant": "acme"}
    assert client.get("/deep/async-tenant", headers={"x-org": "acme"}).json() == {"tenant": "acme"}
    assert client.get("/deep/headers", headers={"x-org": "acme"}).json() == {"tenant": "acme"}
    assert client.post("/deep/upload", files={"file": ("hello.txt", b"hi")}).json() == {
        "filename": "hello.txt"
    }
    assert client.post("/deep/optional-upload").json() == {"filename": None}


def test_serialization_exclude_and_expose_groups():
    client = TestClient(FaNestFactory.create(DeepModule))

    assert client.get("/serialize-deep/public").json() == {"email": "ada@example.com"}
    assert client.get("/serialize-deep/admin").json() == {
        "email": "ada@example.com",
        "token": "admin-token",
    }


def test_uri_header_media_and_custom_versioning_strategies():
    uri_client = TestClient(FaNestFactory.create(DeepModule, versioning=True))
    assert uri_client.get("/v1/versioned/uri").json() == {"strategy": "uri"}
    assert uri_client.get("/v2/versioned/uri").json() == {"strategy": "uri"}

    header_client = TestClient(
        FaNestFactory.create(
            DeepModule,
            versioning={"type": VersioningType.HEADER, "header": "x-api-version"},
        )
    )
    assert header_client.get("/versioned/selected", headers={"x-api-version": "3"}).json() == {
        "strategy": "selected"
    }
    assert header_client.get("/versioned/selected").status_code == 404

    media_client = TestClient(
        FaNestFactory.create(
            DeepModule,
            versioning={"type": VersioningType.MEDIA_TYPE, "key": "version"},
        )
    )
    assert media_client.get(
        "/versioned/selected",
        headers={"accept": "application/json; version=3"},
    ).json() == {"strategy": "selected"}

    custom_client = TestClient(
        FaNestFactory.create(
            DeepModule,
            versioning={
                "type": VersioningType.CUSTOM,
                "extractor": lambda request: request.query_params.get("version"),
            },
        )
    )
    assert custom_client.get("/versioned/selected?version=3").json() == {"strategy": "selected"}


def test_non_uri_versioning_reports_unsupported_same_path_dispatch():
    @Controller("version-collision")
    class CollisionController:
        @Version("1")
        @Get("/")
        async def first(self):
            return {"version": "1"}

        @Version("2")
        @Get("/")
        async def second(self):
            return {"version": "2"}

    @Module(controllers=[CollisionController])
    class CollisionModule:
        pass

    try:
        FaNestFactory.create(
            CollisionModule,
            versioning={"type": VersioningType.HEADER, "header": "x-api-version"},
        )
    except RuntimeError as exc:
        assert "do not yet support multiple handlers" in str(exc)
    else:
        raise AssertionError("Header versioning should report unsupported same-path dispatch")
