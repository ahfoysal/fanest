from fastapi.testclient import TestClient

from fanest import (
    Controller,
    Cookie,
    FaNestFactory,
    Get,
    HttpCode,
    Module,
    Post,
    Redirect,
    SetMetadata,
    UploadedFile,
    create_param_decorator,
)


CurrentTenant = create_param_decorator(
    lambda data, context: context.request.headers.get(data or "x-tenant", "public")
)


@Controller("deep")
class DeepController:
    @HttpCode(204)
    @Post("empty")
    async def empty(self):
        return None

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

    @Post("upload")
    async def upload(self, file=UploadedFile()):
        return {"filename": file.filename}


@Module(controllers=[DeepController])
class DeepModule:
    pass


def test_http_metadata_cookie_custom_param_redirect_and_upload():
    client = TestClient(FaNestFactory.create(DeepModule))

    assert client.post("/deep/empty").status_code == 204
    assert client.get("/deep/redirect", follow_redirects=False).headers["location"] == "/deep/target"
    assert client.get("/deep/cookie", cookies={"session": "abc"}).json() == {"session": "abc"}
    assert client.get("/deep/tenant", headers={"x-org": "acme"}).json() == {"tenant": "acme"}
    assert client.post("/deep/upload", files={"file": ("hello.txt", b"hi")}).json() == {
        "filename": "hello.txt"
    }
