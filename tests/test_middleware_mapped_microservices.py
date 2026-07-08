import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fanest import (
    ClassSerializerInterceptor,
    Controller,
    FaNestFactory,
    Get,
    MiddlewareConsumer,
    Module,
    Post,
    Injectable,
    PartialType,
    Serialize,
    UseInterceptors,
)
from fanest.microservices import EventPattern, MessagePattern, MicroserviceServer, RedisTransport
from fanest.microservices import (
    ClientProxy,
    ClientsModule,
    InjectClient,
    MicroservicePatternError,
    Transport,
)


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


class ScopedHeaderMiddleware:
    async def use(self, request, call_next):
        response = await call_next(request)
        response.headers["x-scoped"] = "yes"
        return response


class PostOnlyMiddleware:
    async def use(self, request, call_next):
        response = await call_next(request)
        response.headers["x-post-only"] = "yes"
        return response


@Controller("scoped")
class ScopedMiddlewareController:
    @Get("/")
    async def index(self):
        return {"route": "index"}

    @Get("/skip")
    async def skip(self):
        return {"route": "skip"}

    @Post("/post-only")
    async def post_only(self):
        return {"route": "post"}

    @Get("/post-only")
    async def get_post_only(self):
        return {"route": "get"}


@Module(controllers=[ScopedMiddlewareController])
class ScopedMiddlewareModule:
    def configure(self, consumer: MiddlewareConsumer):
        consumer.apply(ScopedHeaderMiddleware).exclude("/scoped/skip").for_routes("/scoped*")
        consumer.apply(PostOnlyMiddleware).for_routes("/scoped/post-only", methods=["POST"])


def test_middleware_consumer_supports_routes_exclusions_and_methods():
    client = TestClient(FaNestFactory.create(ScopedMiddlewareModule))

    assert client.get("/scoped").headers["x-scoped"] == "yes"
    assert "x-scoped" not in client.get("/scoped/skip").headers
    assert client.post("/scoped/post-only").headers["x-post-only"] == "yes"
    assert "x-post-only" not in client.get("/scoped/post-only").headers


def test_partial_type_makes_fields_optional():
    PartialUser = PartialType(UserDto)
    dto = PartialUser(name="Ada")

    assert dto.name == "Ada"
    assert dto.password is None


class MathService:
    events: list[int] = []
    transports: list[str] = []

    @MessagePattern("math.double")
    async def double(self, data, context):
        self.transports.append(context.transport)
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


@Injectable(scope="request")
class ScopedMathService:
    created = 0

    def __init__(self):
        type(self).created += 1
        self.instance_id = type(self).created

    @MessagePattern("scoped.id")
    async def scoped_id(self, data, context):
        return {"id": self.instance_id, "transport": context.transport}


@Module(providers=[ScopedMathService])
class ScopedMathModule:
    pass


@pytest.mark.anyio
async def test_microservice_handlers_resolve_inside_message_scope():
    ScopedMathService.created = 0
    server = MicroserviceServer(ScopedMathModule).compile()
    client = server.client()

    assert await client.send("scoped.id", {}) == {"id": 1, "transport": "memory"}
    assert await client.send("scoped.id", {}) == {"id": 2, "transport": "memory"}


@pytest.mark.anyio
async def test_microservice_named_transports_preserve_context():
    server = MicroserviceServer(MathModule, transport=RedisTransport()).compile()
    client = server.client()

    assert await client.send("math.double", 2) == 4
    assert server.container.resolve(MathService).transports[-1] == "redis"


@pytest.mark.anyio
async def test_microservice_server_create_selects_transport_by_name():
    server = MicroserviceServer.create(MathModule, transport=Transport.KAFKA).compile()

    assert await server.client().send("math.double", 3) == 6
    assert server.container.resolve(MathService).transports[-1] == "kafka"


@pytest.mark.anyio
async def test_microservice_client_proxy_lifecycle_and_missing_pattern_error():
    server = MicroserviceServer(MathModule).compile()
    client = server.client()

    assert client.connected is False
    assert await client.send("math.double", 4) == 8
    assert client.connected is True
    await client.close()
    assert client.connected is False
    with pytest.raises(MicroservicePatternError):
        await client.send("missing", None)


class ClientConsumer:
    def __init__(self, client: ClientProxy = InjectClient("math")):
        self.client = client


@Module(
    imports=[ClientsModule.register({"name": "math", "transport": Transport.MEMORY})],
    providers=[ClientConsumer],
)
class ClientsAppModule:
    pass


def test_clients_module_registers_injectable_client_proxy():
    app = FaNestFactory.create(ClientsAppModule)
    consumer = app.state.fanest_container.resolve(ClientConsumer)

    assert isinstance(consumer.client, ClientProxy)
