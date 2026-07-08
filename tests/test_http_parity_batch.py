from pydantic import BaseModel

from fastapi.testclient import TestClient

from fanest import (
    All,
    BackgroundTasks,
    Controller,
    FaNestFactory,
    Form,
    Get,
    Head,
    HostParam,
    Ip,
    Module,
    Options,
    ResponseModel,
    Session,
    Version,
)


class PublicDto(BaseModel):
    name: str


@Version("1")
@Controller("parity")
class HttpParityController:
    tasks: list[str] = []

    @Head("/")
    async def head(self):
        return {"ignored": True}

    @Options("/")
    async def options(self):
        return {"ok": True}

    @All("/any")
    async def any_method(self):
        return {"ok": True}

    @ResponseModel(PublicDto)
    @Get("/model")
    async def model(self):
        return {"name": "Ada", "password": "hidden"}

    @Get("/ip")
    async def ip(self, ip: str | None = Ip()):
        return {"ip": ip}

    @Get("/session")
    async def session(self, session=Session(default={})):
        return {"session": session}

    @Get("/task")
    async def task(self, background_tasks=BackgroundTasks()):
        background_tasks.add_task(self.tasks.append, "ran")
        return {"queued": True}

    @Get("/form")
    async def form(self, name: str = Form()):
        return {"name": name}


@Controller("tenant", host=":account.example.com")
class HostController:
    @Get("/")
    async def index(self, account: str = HostParam("account")):
        return {"account": account}


@Module(controllers=[HttpParityController, HostController])
class HttpParityModule:
    pass


def test_http_method_response_model_version_ip_session_background_and_form():
    HttpParityController.tasks = []
    client = TestClient(FaNestFactory.create(HttpParityModule))

    assert client.head("/v1/parity").status_code == 200
    assert client.options("/v1/parity").json() == {"ok": True}
    assert client.delete("/v1/parity/any").json() == {"ok": True}
    assert client.get("/v1/parity/model").json() == {"name": "Ada"}
    assert "ip" in client.get("/v1/parity/ip").json()
    assert client.get("/v1/parity/session").json() == {"session": {}}
    assert client.get("/v1/parity/task").json() == {"queued": True}
    assert HttpParityController.tasks == ["ran"]
    assert client.request("GET", "/v1/parity/form", data={"name": "Ada"}).json() == {"name": "Ada"}
    assert client.get("/tenant", headers={"host": "acme.example.com"}).json() == {"account": "acme"}
