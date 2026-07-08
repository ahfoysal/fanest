import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fanest import (
    ClassSerializerInterceptor,
    Controller,
    FaNestFactory,
    Get,
    Module,
    PartialType,
    Serialize,
    UseInterceptors,
)
from fanest.microservices import EventPattern, MessagePattern, MicroserviceServer


class HeaderMiddleware:
    async def use(self, request, call_next):
        response = await call_next(request)
        response.headers["x-fanest"] = "yes"
        return response


class UserDto(BaseModel):
    name: str
    password: str


@Controller("serialize")
class SerializeController:
    @UseInterceptors(ClassSerializerInterceptor)
    @Serialize(exclude={"password"})
    @Get("/")
    async def index(self):
        return UserDto(name="Ada", password="secret")


@Module(controllers=[SerializeController], middlewares=[HeaderMiddleware])
class SerializeModule:
    pass


def test_middleware_and_serializer_interceptor():
    response = TestClient(FaNestFactory.create(SerializeModule)).get("/serialize")

    assert response.headers["x-fanest"] == "yes"
    assert response.json() == {"name": "Ada"}


def test_partial_type_makes_fields_optional():
    PartialUser = PartialType(UserDto)
    dto = PartialUser(name="Ada")

    assert dto.name == "Ada"
    assert dto.password is None


class MathService:
    events: list[int] = []

    @MessagePattern("math.double")
    async def double(self, data, context):
        return data * 2

    @EventPattern("math.seen")
    async def seen(self, data, context):
        self.events.append(data)


@Module(providers=[MathService])
class MathModule:
    pass


@pytest.mark.anyio
async def test_microservice_message_and_event_patterns():
    server = MicroserviceServer(MathModule).compile()
    client = server.client()

    assert await client.send("math.double", 21) == 42
    await client.emit("math.seen", 7)

    service = server.container.resolve(MathService)
    assert service.events == [7]
