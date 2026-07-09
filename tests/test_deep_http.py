from fastapi.testclient import TestClient

from fanest import (
    Controller,
    Cookie,
    FaNestFactory,
    Get,
    Headers,
    HttpCode,
    Module,
    Post,
    Redirect,
    SetHeader,
    SetMetadata,
    UploadedFile,
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


@Module(controllers=[DeepController])
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
