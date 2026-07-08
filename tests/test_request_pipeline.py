from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fanest import (
    Body,
    Controller,
    FaNestFactory,
    Get,
    Injectable,
    Module,
    Param,
    Post,
    Query,
    Req,
    State,
    UseFilters,
    UseGuards,
    UseInterceptors,
    UsePipes,
)


class CreateUserDto(BaseModel):
    name: str


@Injectable()
class UsersService:
    def find_one(self, user_id: int, verbose: bool = False):
        return {"id": user_id, "verbose": verbose}

    def create(self, dto: CreateUserDto):
        return {"name": dto.name}


class AllowGuard:
    def can_activate(self, context):
        return context.request.headers.get("x-deny") != "1"


class TrimPipe:
    def transform(self, value, metadata):
        if isinstance(value, CreateUserDto):
            return CreateUserDto(name=value.name.strip())
        return value


class AddOnePipe:
    def transform(self, value, metadata):
        assert metadata["source"] == "query"
        return value + 1


class TrackingPipe:
    seen: list[str] = []

    def transform(self, value, metadata):
        self.seen.append(metadata["name"])
        return value


class WrapInterceptor:
    async def intercept(self, context, call_next):
        result = await call_next()
        return {"data": result}


class HttpErrorFilter:
    def catch(self, exc, context):
        if isinstance(exc, RequestValidationError):
            return {"error": "validation", "count": len(exc.errors())}
        if isinstance(exc, HTTPException):
            return {"error": exc.detail}
        raise exc


@UseGuards(AllowGuard)
@UseFilters(HttpErrorFilter)
@Controller("users")
class UsersController:
    def __init__(self, users_service: UsersService):
        self.users_service = users_service

    @UseInterceptors(WrapInterceptor)
    @Get("/{user_id}")
    async def find_one(
        self,
        user_id: int = Param(),
        verbose: bool = Query(default=False),
    ):
        return self.users_service.find_one(user_id, verbose)

    @UsePipes(TrimPipe)
    @Post("/")
    async def create(self, dto: CreateUserDto = Body()):
        return self.users_service.create(dto)

    @Get("/blocked")
    async def blocked(self):
        raise HTTPException(status_code=418, detail="blocked")

    @Get("/param-pipe")
    async def param_pipe(self, value: int = Query("value", AddOnePipe())):
        return {"value": value}

    @UsePipes(TrackingPipe())
    @Get("/framework-params")
    async def framework_params(self, request=Req(), user: dict = State("user"), value: str = Query()):
        request.state.user = user or {"sub": "local"}
        return {"value": value, "user": request.state.user}


@Module(controllers=[UsersController], providers=[UsersService])
class UsersModule:
    pass


def test_param_query_body_pipes_guards_interceptors_and_filters():
    client = TestClient(FaNestFactory.create(UsersModule))

    assert client.get("/users/7?verbose=true").json() == {
        "data": {"id": 7, "verbose": True}
    }
    assert client.post("/users", json={"name": " Ada "}).json() == {"name": "Ada"}
    assert client.get("/users/7", headers={"x-deny": "1"}).json() == {"error": "Forbidden"}
    assert client.get("/users/blocked").json() == {"error": "blocked"}
    assert client.get("/users/param-pipe?value=4").json() == {"value": 5}
    assert client.post("/users", json={"name": 123}).json() == {
        "error": "validation",
        "count": 1,
    }
    TrackingPipe.seen = []
    assert client.get("/users/framework-params?value=ok").json() == {
        "value": "ok",
        "user": {"sub": "local"},
    }
    assert TrackingPipe.seen == ["value"]
