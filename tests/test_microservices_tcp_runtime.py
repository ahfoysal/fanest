import asyncio

import pytest

from fanest import FaNestFactory, Module
from fanest.microservices import (
    ClientProxy,
    ClientProxyFactory,
    EventPattern,
    MessagePattern,
    MicroserviceRemoteError,
    MicroserviceServer,
    MicroserviceTimeoutError,
    TcpContext,
    TcpTransport,
    Transport,
)


class TcpProbeService:
    events: list[dict[str, object]] = []

    @MessagePattern("math.double")
    async def double(self, data, context: TcpContext):
        return {
            "value": data["value"] * 2,
            "transport": context.transport,
            "remote_address": context.remote_address,
            "remote_port": context.remote_port,
            "correlation_id": context.correlation_id,
            "reply_to": context.reply_to,
        }

    @MessagePattern("boom")
    async def boom(self, data, context):
        raise ValueError("nope")

    @MessagePattern("slow")
    async def slow(self, data, context):
        await asyncio.sleep(1)
        return data

    @EventPattern("seen")
    async def seen(self, data, context: TcpContext):
        self.events.append(
            {
                "data": data,
                "transport": context.transport,
                "remote_address": context.remote_address,
                "correlation_id": context.correlation_id,
            }
        )


@Module(providers=[TcpProbeService])
class TcpProbeModule:
    pass


@pytest.mark.anyio
async def test_tcp_transport_request_reply_emit_and_context_lifecycle():
    TcpProbeService.events = []
    server_transport = TcpTransport(port=0)
    server = MicroserviceServer(TcpProbeModule, transport=server_transport)
    await server.listen()
    client = ClientProxy(TcpTransport(port=server_transport.port))

    try:
        response = await client.send("math.double", {"value": 21})
        await client.emit("seen", {"id": 7})
        await asyncio.sleep(0.05)
    finally:
        await client.close()
        await server.close()

    assert response["value"] == 42
    assert response["transport"] == "tcp"
    assert response["remote_address"] in {"127.0.0.1", "::1"}
    assert isinstance(response["remote_port"], int)
    assert response["correlation_id"]
    assert response["reply_to"] == "tcp"
    assert TcpProbeService.events == [
        {
            "data": {"id": 7},
            "transport": "tcp",
            "remote_address": response["remote_address"],
            "correlation_id": TcpProbeService.events[0]["correlation_id"],
        }
    ]


@pytest.mark.anyio
async def test_tcp_transport_remote_errors_and_client_proxy_timeout():
    server_transport = TcpTransport(port=0)
    server = MicroserviceServer(TcpProbeModule, transport=server_transport)
    await server.listen()
    client = ClientProxy(TcpTransport(port=server_transport.port))
    timeout_client = ClientProxy(TcpTransport(port=server_transport.port), timeout=0.01)

    try:
        with pytest.raises(MicroserviceRemoteError) as exc_info:
            await client.send("boom", {})
        with pytest.raises(MicroserviceTimeoutError):
            await timeout_client.send("slow", {"value": 1})
    finally:
        await timeout_client.close()
        await client.close()
        await server.close()

    assert str(exc_info.value) == "nope"
    assert exc_info.value.error_type == "ValueError"


class PrefixSerializer:
    def serialize(self, value):
        import json

        return ("fanest:" + json.dumps(value)).encode()

    def deserialize(self, value):
        import json

        if isinstance(value, bytes):
            value = value.decode()
        assert value.startswith("fanest:")
        return json.loads(value.removeprefix("fanest:"))


@pytest.mark.anyio
async def test_tcp_transport_uses_serializer_deserializer_hooks_and_factory_selection():
    serializer = PrefixSerializer()
    server_transport = TcpTransport(port=0, serializer=serializer, deserializer=serializer)
    server = MicroserviceServer(TcpProbeModule, transport=server_transport)
    await server.listen()
    client = ClientProxyFactory.create(
        transport=Transport.TCP,
        port=server_transport.port,
        serializer=serializer,
        deserializer=serializer,
    )

    try:
        response = await client.send("math.double", {"value": 5})
    finally:
        await client.close()
        await server.close()

    assert response["value"] == 10


@pytest.mark.anyio
async def test_hybrid_app_lifecycle_starts_and_closes_tcp_microservice():
    app = FaNestFactory.create(TcpProbeModule)
    server = app.connect_microservice({"transport": Transport.TCP, "port": 0})

    await app.start_all_microservices()
    assert server.transport.connected is True
    assert isinstance(server.transport, TcpTransport)
    assert server.transport.port > 0

    await app.close_all_microservices()
    assert server.transport.connected is False
