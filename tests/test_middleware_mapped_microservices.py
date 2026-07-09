import asyncio
import importlib.util
import os

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, field_validator

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
    PickType,
    Serialize,
    UseInterceptors,
)
from fanest.microservices import EventPattern, MessagePattern, MicroserviceServer, RedisTransport
from fanest.microservices import (
    ClientProxy,
    ClientProxyFactory,
    ClientsModule,
    GrpcTransport,
    GrpcProtoLoader,
    InjectClient,
    KafkaTransport,
    MicroserviceContext,
    MicroserviceDuplicateHandlerError,
    MicroserviceEventError,
    MicroservicePatternError,
    MicroserviceRemoteError,
    MicroserviceTimeoutError,
    MicroserviceTransportError,
    NatsContext,
    NatsTransport,
    RabbitMqTransport,
    Transport,
    serialize_pattern,
)


class HeaderMiddleware:
    async def use(self, request, call_next):
        response = await call_next(request)
        response.headers["x-fanest"] = "yes"
        return response


class UserDto(BaseModel):
    name: str
    password: str


class StrictUserDto(BaseModel):
    name: str = Field(min_length=3)
    tags: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def no_root(cls, value: str) -> str:
        if value == "root":
            raise ValueError("reserved")
        return value


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

    assert getattr(dto, "name") == "Ada"
    assert getattr(dto, "password") is None


def test_mapped_types_preserve_validation_and_default_factory():
    PartialStrictUser = PartialType(StrictUserDto)
    PickStrictUser = PickType(StrictUserDto, ["name", "tags"])

    assert getattr(PartialStrictUser(), "tags") is None
    assert getattr(PickStrictUser(name="Ada"), "tags") == []

    with pytest.raises(ValueError):
        PickStrictUser(name="Al")

    with pytest.raises(ValueError):
        PickStrictUser(name="root")


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


class FakeAsyncRedis:
    def __init__(self):
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.deleted: list[str] = []
        self.pinged = False
        self.closed = False
        self._sequence = 0

    async def ping(self):
        self.pinged = True

    async def aclose(self):
        self.closed = True

    async def xadd(self, stream, fields):
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def xread(self, streams, *, block=0, count=1):
        for stream, last_id in streams.items():
            messages = [
                (message_id, fields)
                for message_id, fields in self.streams.get(stream, [])
                if self._after(message_id, last_id)
            ]
            if messages:
                return [(stream, messages[:count])]
        return []

    async def delete(self, stream):
        self.deleted.append(stream)
        self.streams.pop(stream, None)

    def _after(self, message_id: str, last_id: str) -> bool:
        if last_id in {"$", "0-0"}:
            return last_id == "0-0"
        return int(message_id.split("-", 1)[0]) > int(last_id.split("-", 1)[0])


class FakeBrokerAdapter:
    def __init__(self):
        self.connected = False
        self.closed = False
        self.sent: list[tuple[str, object]] = []
        self.emitted: list[tuple[str, object]] = []

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def send(self, pattern: str, data):
        self.sent.append((pattern, data))
        return {"pattern": pattern, "data": data}

    async def emit(self, pattern: str, data):
        self.emitted.append((pattern, data))


@pytest.mark.anyio
async def test_microservice_message_and_event_patterns():
    server = MicroserviceServer(MathModule).compile()
    client = server.client()

    assert await client.send("math.double", 21) == 42
    await client.emit("math.seen", 7)

    service = server.container.resolve(MathService)
    assert service.events == [7]


class ObjectPatternService:
    seen: list[str] = []

    @MessagePattern({"cmd": "sum", "version": 1})
    async def sum_values(self, data, context):
        return {
            "total": sum(data),
            "pattern": context.pattern,
            "raw": context.raw_pattern,
        }

    @EventPattern({"event": "object.seen"})
    async def object_seen(self, data, context):
        self.seen.append(context.raw_pattern)


@Module(providers=[ObjectPatternService])
class ObjectPatternModule:
    pass


@pytest.mark.anyio
async def test_microservice_supports_stable_serialized_object_patterns():
    server = MicroserviceServer(ObjectPatternModule).compile()
    client = server.client()

    response = await client.send({"version": 1, "cmd": "sum"}, [2, 3])
    await client.emit({"event": "object.seen"}, {"id": 1})

    assert response == {
        "total": 5,
        "pattern": {"cmd": "sum", "version": 1},
        "raw": '{"cmd":"sum","version":1}',
    }
    assert server.container.resolve(ObjectPatternService).seen == ['{"event":"object.seen"}']
    assert serialize_pattern({"version": 1, "cmd": "sum"}) == '{"cmd":"sum","version":1}'


class DuplicateOne:
    @MessagePattern("duplicate")
    async def handle(self, data, context):
        return data


class DuplicateTwo:
    @MessagePattern("duplicate")
    async def handle(self, data, context):
        return data


@Module(providers=[DuplicateOne, DuplicateTwo])
class DuplicateMessageModule:
    pass


def test_microservice_rejects_duplicate_message_handlers():
    with pytest.raises(MicroserviceDuplicateHandlerError):
        MicroserviceServer(DuplicateMessageModule).compile()


class FailingEventService:
    seen: list[str] = []

    @EventPattern("fanout")
    async def first(self, data, context):
        self.seen.append("first")
        raise RuntimeError("first failed")

    @EventPattern("fanout")
    async def second(self, data, context):
        self.seen.append("second")


@Module(providers=[FailingEventService])
class FailingEventModule:
    pass


@pytest.mark.anyio
async def test_microservice_event_fanout_runs_all_handlers_before_raising():
    FailingEventService.seen = []
    server = MicroserviceServer(FailingEventModule).compile()

    with pytest.raises(MicroserviceEventError) as exc_info:
        await server.client().emit("fanout", {})

    assert FailingEventService.seen == ["first", "second"]
    assert len(exc_info.value.errors) == 1


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
async def test_redis_transport_processes_stream_requests_and_events():
    MathService.events = []
    fake = FakeAsyncRedis()
    transport = RedisTransport()
    transport._client = fake
    server = MicroserviceServer(MathModule, transport=transport).compile()

    await fake.xadd(
        "fanest:microservice:requests",
        {
            "id": "request-1",
            "pattern": "math.double",
            "data": "5",
            "reply_to": "fanest:microservice:reply:request-1",
        },
    )
    last_request_id, last_event_id = await transport.listen_once(last_request_id="0-0")

    assert fake.streams["fanest:microservice:reply:request-1"][0][1]["data"] == "10"

    await fake.xadd(
        "fanest:microservice:events",
        {"pattern": "math.seen", "data": "11"},
    )
    await transport.listen_once(last_request_id=last_request_id, last_event_id=last_event_id)

    assert server.container.resolve(MathService).events[-1] == 11


@pytest.mark.anyio
async def test_redis_transport_returns_error_envelopes_for_failed_requests():
    fake = FakeAsyncRedis()
    transport = RedisTransport()
    transport._client = fake
    MicroserviceServer(MathModule, transport=transport).compile()

    await fake.xadd(
        "fanest:microservice:requests",
        {
            "id": "request-1",
            "pattern": "missing",
            "data": "5",
            "reply_to": "fanest:microservice:reply:request-1",
        },
    )
    await transport.listen_once(last_request_id="0-0")

    payload = fake.streams["fanest:microservice:reply:request-1"][0][1]
    assert payload["data"] == "null"
    assert payload["error_type"] == "MicroservicePatternError"
    assert "No message handler registered" in payload["error"]


@pytest.mark.anyio
async def test_redis_client_proxy_raises_remote_errors_from_reply_envelope():
    fake = FakeAsyncRedis()
    transport = RedisTransport()
    transport._client = fake
    client = ClientProxy(transport)

    async def xread(streams, *, block=0, count=1):
        reply_stream = next(iter(streams))
        return [
            (
                reply_stream,
                [
                    (
                        "1-0",
                        {
                            "data": "null",
                            "error": "boom",
                            "error_type": "ValueError",
                        },
                    )
                ],
            )
        ]

    fake.xread = xread

    with pytest.raises(MicroserviceRemoteError) as exc_info:
        await client.send("remote.fail", {})

    assert str(exc_info.value) == "boom"
    assert exc_info.value.error_type == "ValueError"


@pytest.mark.anyio
async def test_client_proxy_calls_transport_connect_and_close_hooks():
    fake = FakeAsyncRedis()
    transport = RedisTransport()
    transport._client = fake
    client = ClientProxy(transport)

    await client.connect()
    await client.close()

    assert fake.pinged is True
    assert fake.closed is True


@pytest.mark.anyio
async def test_redis_transport_accepts_client_hook_for_stream_integration():
    fake = FakeAsyncRedis()
    transport = RedisTransport(client=fake, prefix="fanest:test-micro:")
    server = MicroserviceServer(MathModule, transport=transport)

    await server.listen()
    await fake.xadd(
        "fanest:test-micro:requests",
        {
            "id": "request-1",
            "pattern": "math.double",
            "data": "9",
            "reply_to": "fanest:test-micro:reply:request-1",
        },
    )
    await transport.listen_once(last_request_id="0-0")
    await server.close()

    assert fake.streams["fanest:test-micro:reply:request-1"][0][1]["data"] == "18"
    assert fake.pinged is True
    assert fake.closed is True


@pytest.mark.live_redis
@pytest.mark.skipif(not os.getenv("FANEST_LIVE_REDIS"), reason="set FANEST_LIVE_REDIS to run live Redis checks")
@pytest.mark.anyio
async def test_live_redis_transport_connects_when_enabled():
    transport = RedisTransport(
        url=os.getenv("FANEST_LIVE_REDIS_URL", "redis://localhost:6379/0"),
        prefix="fanest:live-micro:",
    )

    await transport.connect()
    await transport.close()

    assert transport.connected is False


@pytest.mark.anyio
async def test_network_transports_delegate_to_custom_adapter_for_broker_integrations():
    adapter = FakeBrokerAdapter()
    client = ClientProxy(KafkaTransport(adapter=adapter))

    assert await client.send("math.double", 6) == {"pattern": "math.double", "data": 6}
    await client.emit("math.seen", 7)
    await client.close()

    assert adapter.connected is True
    assert adapter.closed is True
    assert adapter.sent == [("math.double", 6)]
    assert adapter.emitted == [("math.seen", 7)]


@pytest.mark.anyio
async def test_client_proxy_factory_accepts_nest_style_options_object():
    adapter = FakeBrokerAdapter()
    client = ClientProxyFactory.create(
        {
            "transport": Transport.KAFKA,
            "options": {"adapter": adapter},
        }
    )

    assert await client.send("broker.echo", {"ok": True}) == {
        "pattern": "broker.echo",
        "data": {"ok": True},
    }
    await client.close()

    assert adapter.connected is True
    assert adapter.closed is True


@pytest.mark.anyio
async def test_microservice_server_listen_and_close_manage_transport_lifecycle():
    fake = FakeAsyncRedis()
    transport = RedisTransport()
    transport._client = fake
    server = MicroserviceServer(MathModule, transport=transport)

    await server.listen()
    await server.close()

    assert fake.pinged is True
    assert fake.closed is True
    assert transport.connected is False


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


class UpperJsonSerializer:
    def serialize(self, value):
        import json

        return json.dumps(value).upper().encode()

    def deserialize(self, value):
        import json

        if isinstance(value, bytes):
            value = value.decode()
        return json.loads(value.lower())


class ContextProbeService:
    @MessagePattern("ctx")
    async def handle(self, data, context: MicroserviceContext):
        return {
            "transport": context.transport,
            "headers": context.headers,
            "correlation_id": context.correlation_id,
            "reply_to": context.reply_to,
        }


@Module(providers=[ContextProbeService])
class ContextProbeModule:
    pass


@pytest.mark.anyio
async def test_redis_transport_preserves_headers_and_correlation_context():
    fake = FakeAsyncRedis()
    transport = RedisTransport()
    transport._client = fake
    MicroserviceServer(ContextProbeModule, transport=transport).compile()

    await fake.xadd(
        "fanest:microservice:requests",
        {
            "id": "corr-1",
            "pattern": "ctx",
            "data": "{}",
            "headers": '{"x-request-id":"abc"}',
            "reply_to": "fanest:microservice:reply:corr-1",
        },
    )
    await transport.listen_once(last_request_id="0-0")

    payload = fake.streams["fanest:microservice:reply:corr-1"][0][1]
    assert '"correlation_id": "corr-1"' in payload["data"]
    assert '"reply_to": "fanest:microservice:reply:corr-1"' in payload["data"]


class NeverReplyTransport(RedisTransport):
    async def send(self, pattern, data):
        await asyncio.sleep(1)
        return data


class FlakyTransport(RedisTransport):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def send(self, pattern, data):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary")
        return {"ok": data}


@pytest.mark.anyio
async def test_client_proxy_timeout_and_retry_options():
    timeout_client = ClientProxy(NeverReplyTransport(), timeout=0.01)
    with pytest.raises(MicroserviceTimeoutError):
        await timeout_client.send("slow", {})

    flaky = FlakyTransport()
    retry_client = ClientProxy(flaky, retries=1)
    assert await retry_client.send("retry", 3) == {"ok": 3}
    assert flaky.calls == 2


class FakeNatsMessage:
    def __init__(self, subject, data, reply=None):
        self.subject = subject
        self.data = data
        self.reply = reply


class FakeNatsClient:
    def __init__(self):
        self.connected = True
        self.requests: list[tuple[str, bytes]] = []
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: list[tuple[str, object]] = []
        self.drained = False

    async def request(self, subject, payload):
        self.requests.append((subject, payload))
        return FakeNatsMessage(subject, b'{"answer":42}')

    async def publish(self, subject, payload):
        self.published.append((subject, payload))

    async def subscribe(self, subject, cb):
        self.subscriptions.append((subject, cb))
        return object()

    async def drain(self):
        self.drained = True


@pytest.mark.anyio
async def test_nats_transport_real_client_shape_and_context():
    client = FakeNatsClient()
    transport = NatsTransport(client=client, subject_prefix="svc.")
    assert await ClientProxy(transport).send("math.answer", {"q": 1}) == {"answer": 42}
    await transport.emit("math.event", {"seen": True})

    assert client.requests == [("svc.math.answer", b'{"q": 1}')]
    assert client.published == [("svc.math.event", b'{"seen": true}')]

    seen: list[NatsContext] = []

    class NatsService:
        @MessagePattern("ctx")
        async def handle(self, data, context):
            seen.append(context)
            return {"ok": True}

    @Module(providers=[NatsService])
    class NatsModule:
        pass

    MicroserviceServer(NatsModule, transport=transport).compile()
    await transport._handle_nats_message(FakeNatsMessage("svc.ctx", b'{"hello":true}', reply="inbox"))

    assert isinstance(seen[-1], NatsContext)
    assert seen[-1].subject == "svc.ctx"
    assert client.published[-1] == ("inbox", b'{"ok": true}')


class FakeKafkaProducer:
    def __init__(self, broker=None):
        self.started = False
        self.stopped = False
        self.sent: list[tuple[str, bytes, bytes]] = []
        self.broker = broker

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send_and_wait(self, topic, value, key=None):
        self.sent.append((topic, value, key))
        if self.broker is not None:
            await self.broker.publish(topic, value, key)


class FakeKafkaMessage:
    def __init__(self, topic, value, key=None, partition=0, offset=0):
        self.topic = topic
        self.value = value
        self.key = key
        self.partition = partition
        self.offset = offset


class FakeKafkaBroker:
    def __init__(self):
        self.queues: dict[str, asyncio.Queue[FakeKafkaMessage]] = {}
        self.offset = 0

    async def publish(self, topic, value, key=None):
        self.offset += 1
        await self.queues.setdefault(topic, asyncio.Queue()).put(
            FakeKafkaMessage(topic, value, key=key, offset=self.offset)
        )

    def queue(self, topic):
        return self.queues.setdefault(topic, asyncio.Queue())


class FakeKafkaConsumer:
    def __init__(self, broker: FakeKafkaBroker, topic: str):
        self.broker = broker
        self.topic = topic
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.broker.queue(self.topic).get()


@pytest.mark.anyio
async def test_kafka_transport_emit_and_requires_reply_consumer_for_request_reply():
    producer = FakeKafkaProducer()
    transport = KafkaTransport(producer=producer, topic="orders")
    client = ClientProxy(transport)

    await client.emit("order.created", {"id": 1})
    assert producer.started is True
    assert producer.sent == [("orders", b'{"id": 1}', b"order.created")]

    with pytest.raises(MicroserviceTransportError):
        await client.send("order.query", {"id": 1})


@pytest.mark.anyio
async def test_kafka_transport_supports_request_reply_with_reply_consumer():
    broker = FakeKafkaBroker()
    server_producer = FakeKafkaProducer(broker)
    client_producer = FakeKafkaProducer(broker)
    server_transport = KafkaTransport(
        producer=server_producer,
        consumer=FakeKafkaConsumer(broker, "orders"),
        topic="orders",
        reply_topic="orders.replies",
    )
    client_transport = KafkaTransport(
        producer=client_producer,
        reply_consumer=FakeKafkaConsumer(broker, "orders.replies"),
        topic="orders",
        reply_topic="orders.replies",
    )
    server = MicroserviceServer(MathModule, transport=server_transport)

    await server.listen()
    try:
        assert await ClientProxy(client_transport, timeout=1).send("math.double", 8) == 16
    finally:
        await client_transport.close()
        await server.close()

    assert client_producer.sent[0][0] == "orders"
    assert server_producer.sent[0][0] == "orders.replies"


class FakeGrpcStub:
    async def Sum(self, data):
        return {"total": sum(data)}


@pytest.mark.anyio
async def test_grpc_transport_invokes_stub_methods_and_reports_missing_methods():
    transport = GrpcTransport(stub=FakeGrpcStub())
    client = ClientProxy(transport)

    assert await client.send("Sum", [2, 3]) == {"total": 5}
    with pytest.raises(MicroservicePatternError):
        await client.send("Missing", {})


def test_grpc_proto_loader_loads_generated_stub_shape():
    class FakeGrpcModule:
        class MathStub:
            def __init__(self, channel):
                self.channel = channel

    stub = GrpcProtoLoader.load_stub(FakeGrpcModule, service="Math", channel="channel")

    assert isinstance(stub, FakeGrpcModule.MathStub)
    assert stub.channel == "channel"


def test_grpc_proto_loader_reports_missing_proto_files(tmp_path):
    missing = tmp_path / "missing.proto"

    with pytest.raises(FileNotFoundError):
        GrpcProtoLoader.compile(missing, output_dir=tmp_path / "out")

    with pytest.raises(FileNotFoundError):
        GrpcProtoLoader.compile_with_subprocess(missing, output_dir=tmp_path / "out")


@pytest.mark.skipif(
    not os.getenv("FANEST_LIVE_GRPC_TOOLS"),
    reason="set FANEST_LIVE_GRPC_TOOLS=1 to run grpc_tools codegen smoke",
)
def test_live_grpc_proto_loader_compiles_when_enabled(tmp_path):
    proto = tmp_path / "math.proto"
    proto.write_text(
        """
        syntax = "proto3";
        package fanest.test;
        service Math { rpc Sum (SumRequest) returns (SumReply); }
        message SumRequest { repeated int32 values = 1; }
        message SumReply { int32 total = 1; }
        """,
        encoding="utf-8",
    )

    artifacts = GrpcProtoLoader.compile(proto, output_dir=tmp_path / "generated")

    assert artifacts.python_module.exists()
    assert artifacts.grpc_module.exists()


@pytest.mark.anyio
async def test_broker_transports_raise_clean_optional_dependency_errors():
    if importlib.util.find_spec("nats") is None:
        with pytest.raises(ImportError, match="nats-py"):
            await NatsTransport(url="nats://localhost:4222").connect()
    if importlib.util.find_spec("aio_pika") is None:
        with pytest.raises(ImportError, match="aio-pika"):
            await RabbitMqTransport(url="amqp://guest:guest@localhost/").connect()
    if importlib.util.find_spec("aiokafka") is None:
        with pytest.raises(ImportError, match="aiokafka"):
            await KafkaTransport(bootstrap_servers="localhost:9092").connect()


@pytest.mark.skipif(
    not os.getenv("FANEST_LIVE_NATS_URL"),
    reason="set FANEST_LIVE_NATS_URL to run live NATS checks",
)
@pytest.mark.anyio
async def test_live_nats_transport_connects_when_enabled():
    transport = NatsTransport(url=os.environ["FANEST_LIVE_NATS_URL"])

    await transport.connect()
    await transport.close()

    assert transport.connected is False


@pytest.mark.skipif(
    not os.getenv("FANEST_LIVE_RABBITMQ_URL"),
    reason="set FANEST_LIVE_RABBITMQ_URL to run live RabbitMQ checks",
)
@pytest.mark.anyio
async def test_live_rabbitmq_transport_connects_when_enabled():
    transport = RabbitMqTransport(url=os.environ["FANEST_LIVE_RABBITMQ_URL"])

    await transport.connect()
    await transport.close()

    assert transport.connected is False


@pytest.mark.anyio
async def test_hybrid_app_connect_start_and_close_microservices():
    app = FaNestFactory.create(MathModule)
    server = app.connect_microservice({"transport": Transport.REDIS})

    assert isinstance(server, MicroserviceServer)
    assert app.state.fanest_microservices == [server]
    await app.start_all_microservices()
    assert server.transport.connected is True
    await app.close_all_microservices()
    assert server.transport.connected is False
