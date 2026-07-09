from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import pytest

from fanest import (
    ConnectedSocket,
    FaNestFactory,
    MessageBody,
    Module,
    SubscribeMessage,
    Catch,
    UseGuards,
    UseFilters,
    UsePipes,
    WebSocketGateway,
    WebSocketManager,
    SocketIoServer,
    ValidationPipe,
    create_param_decorator,
    forward_ref,
    use_factory,
)


@WebSocketGateway("/chat")
class ChatGateway:
    @SubscribeMessage("echo")
    async def echo(self, data, websocket):
        return {"echo": data}


@Module(gateways=[ChatGateway])
class ChatModule:
    pass


def test_websocket_gateway_dispatches_messages():
    client = TestClient(FaNestFactory.create(ChatModule))

    with client.websocket_connect("/chat") as websocket:
        websocket.send_json({"event": "echo", "data": "hello"})
        assert websocket.receive_json() == {"event": "echo", "data": {"echo": "hello"}}


@WebSocketGateway("/rooms")
class RoomGateway:
    def __init__(self, manager: WebSocketManager):
        self.manager = manager

    async def on_connect(self, websocket):
        self.manager.join("general", websocket)

    @SubscribeMessage("publish")
    async def publish(self, data, websocket):
        await self.manager.broadcast("general", "published", data, exclude=websocket)
        return {"sent": True}


@Module(gateways=[RoomGateway])
class RoomModule:
    pass


def test_websocket_manager_supports_rooms_and_broadcasts():
    client = TestClient(FaNestFactory.create(RoomModule))

    with client.websocket_connect("/rooms") as sender:
        with client.websocket_connect("/rooms") as receiver:
            sender.send_json({"event": "publish", "data": {"body": "hello"}})
            assert receiver.receive_json() == {"event": "published", "data": {"body": "hello"}}
            assert sender.receive_json() == {"event": "publish", "data": {"sent": True}}


class WebSocketTokenGuard:
    def can_activate(self, context):
        return context.request.query_params.get("token") == "ok"


class UpperMessagePipe:
    def transform(self, value, metadata):
        return str(value).upper()


@WebSocketGateway("/secure-chat")
@UseGuards(WebSocketTokenGuard)
class SecureChatGateway:
    @SubscribeMessage("shout")
    @UsePipes(UpperMessagePipe())
    async def shout(self, data, websocket):
        return {"message": data}


@Module(gateways=[SecureChatGateway])
class SecureChatModule:
    pass


@WebSocketGateway("/unannotated-body")
@UsePipes(ValidationPipe())
class UnannotatedBodyGateway:
    @SubscribeMessage("ping")
    async def ping(self, data=MessageBody()):
        return {"payload": data}


@Module(gateways=[UnannotatedBodyGateway])
class UnannotatedBodyModule:
    pass


def test_websocket_gateway_runs_guards_and_pipes():
    client = TestClient(FaNestFactory.create(SecureChatModule))

    with pytest.raises(Exception):
        with client.websocket_connect("/secure-chat?token=bad"):
            pass

    with client.websocket_connect("/secure-chat?token=ok") as websocket:
        websocket.send_json({"event": "shout", "data": "hello"})
        assert websocket.receive_json() == {
            "event": "shout",
            "data": {"message": "HELLO"},
        }


def test_unannotated_websocket_message_body_is_treated_as_any_for_pipes():
    client = TestClient(FaNestFactory.create(UnannotatedBodyModule))

    with client.websocket_connect("/unannotated-body") as websocket:
        websocket.send_json({"event": "ping", "data": {"ok": True}})
        assert websocket.receive_json() == {
            "event": "ping",
            "data": {"payload": {"ok": True}},
        }


def test_websocket_gateway_guard_runs_before_on_connect():
    class CountingGuard:
        def can_activate(self, context):
            return False

    @WebSocketGateway("/preauth")
    @UseGuards(CountingGuard)
    class PreAuthGateway:
        connected = 0

        async def on_connect(self, websocket):
            type(self).connected += 1

        @SubscribeMessage("echo")
        async def echo(self, data, websocket):
            return data

    @Module(gateways=[PreAuthGateway])
    class PreAuthModule:
        pass

    client = TestClient(FaNestFactory.create(PreAuthModule))

    with pytest.raises(Exception):
        with client.websocket_connect("/preauth"):
            pass
    assert PreAuthGateway.connected == 0


def test_websocket_gateway_on_connect_method_guard_runs_before_accept():
    class RejectConnectGuard:
        def can_activate(self, context):
            return False

    @WebSocketGateway("/method-preauth")
    class MethodPreAuthGateway:
        connected = 0

        @UseGuards(RejectConnectGuard)
        async def on_connect(self, websocket):
            type(self).connected += 1

        @SubscribeMessage("echo")
        async def echo(self, data, websocket):
            return data

    @Module(providers=[RejectConnectGuard, MethodPreAuthGateway])
    class MethodPreAuthModule:
        pass

    client = TestClient(FaNestFactory.create(MethodPreAuthModule))

    with pytest.raises(Exception):
        with client.websocket_connect("/method-preauth"):
            pass
    assert MethodPreAuthGateway.connected == 0


@WebSocketGateway("/socketio")
class SocketIoGateway:
    def __init__(self, server: SocketIoServer):
        self.server = server

    async def on_connect(self, websocket):
        self.server.join(websocket, "lobby")

    @SubscribeMessage("announce")
    async def announce(self, data, websocket):
        await self.server.to("lobby").emit("announcement", data, exclude=websocket)
        return {"sent": True}


@Module(gateways=[SocketIoGateway])
class SocketIoModule:
    pass


def test_socket_io_style_room_emitter():
    client = TestClient(FaNestFactory.create(SocketIoModule))

    with client.websocket_connect("/socketio") as sender:
        with client.websocket_connect("/socketio") as receiver:
            sender.send_json({"event": "announce", "data": {"text": "hi"}})
            assert receiver.receive_json() == {"event": "announcement", "data": {"text": "hi"}}
            assert sender.receive_json() == {"event": "announce", "data": {"sent": True}}


@Catch(ValueError)
class WebSocketValueErrorFilter:
    def catch(self, exc, context):
        return {"kind": "value", "message": str(exc)}


@WebSocketGateway("/filtered-ws")
@UseFilters(WebSocketValueErrorFilter)
class FilteredGateway:
    @SubscribeMessage("fail")
    async def fail(self, data, websocket):
        raise ValueError("bad socket")


@Module(gateways=[FilteredGateway])
class FilteredWsModule:
    pass


def test_websocket_gateway_uses_exception_filters():
    client = TestClient(FaNestFactory.create(FilteredWsModule))

    with client.websocket_connect("/filtered-ws") as websocket:
        websocket.send_json({"event": "fail", "data": None})
        assert websocket.receive_json() == {
            "event": "error",
            "data": {"kind": "value", "message": "bad socket"},
        }


@Catch(ValueError)
class WebSocketJsonResponseFilter:
    def catch(self, exc, context):
        return JSONResponse(status_code=418, content={"kind": "json-response", "message": str(exc)})


@WebSocketGateway("/response-filter-ws")
@UseFilters(WebSocketJsonResponseFilter)
class ResponseFilterGateway:
    @SubscribeMessage("fail")
    async def fail(self, data, websocket):
        raise ValueError("response object")


@Module(gateways=[ResponseFilterGateway])
class ResponseFilterWsModule:
    pass


def test_websocket_exception_filter_can_return_json_response():
    client = TestClient(FaNestFactory.create(ResponseFilterWsModule))

    with client.websocket_connect("/response-filter-ws") as websocket:
        websocket.send_json({"event": "fail", "data": None})
        assert websocket.receive_json() == {
            "event": "error",
            "data": {"kind": "json-response", "message": "response object"},
        }


@WebSocketGateway("/decorated-socket")
class DecoratedSocketGateway:
    @SubscribeMessage("rename")
    async def rename(self, name: str = MessageBody("name"), websocket=ConnectedSocket()):
        assert websocket is not None
        return {"name": name}


@Module(gateways=[DecoratedSocketGateway])
class DecoratedSocketModule:
    pass


def test_websocket_gateway_supports_message_body_and_connected_socket_decorators():
    client = TestClient(FaNestFactory.create(DecoratedSocketModule))

    with client.websocket_connect("/decorated-socket") as websocket:
        websocket.send_json({"event": "rename", "data": {"name": "Ada"}})
        assert websocket.receive_json() == {"event": "rename", "data": {"name": "Ada"}}


async def async_socket_dependency_factory():
    return "socket-ready"


@WebSocketGateway("/async-factory-socket")
class AsyncFactorySocketGateway:
    def __init__(self, dependency: str = forward_ref(lambda: "SOCKET_DEPENDENCY")):
        self.dependency = dependency

    @SubscribeMessage("ping")
    async def ping(self, data):
        return {"dependency": self.dependency, "data": data}


@Module(
    gateways=[AsyncFactorySocketGateway],
    providers=[use_factory("SOCKET_DEPENDENCY", async_socket_dependency_factory)],
)
class AsyncFactorySocketModule:
    pass


def test_websocket_gateway_awaits_async_factory_dependencies():
    client = TestClient(FaNestFactory.create(AsyncFactorySocketModule))

    with client.websocket_connect("/async-factory-socket") as websocket:
        websocket.send_json({"event": "ping", "data": "hello"})
        assert websocket.receive_json() == {
            "event": "ping",
            "data": {"dependency": "socket-ready", "data": "hello"},
        }


@WebSocketGateway("/provider-listed-socket")
class ProviderListedSocketGateway:
    @SubscribeMessage("ping")
    async def ping(self, data):
        return {"ok": data}


@Module(providers=[ProviderListedSocketGateway])
class ProviderListedSocketModule:
    pass


def test_websocket_gateway_registered_as_provider_is_routable():
    client = TestClient(FaNestFactory.create(ProviderListedSocketModule))

    with client.websocket_connect("/provider-listed-socket") as websocket:
        websocket.send_json({"event": "ping", "data": True})
        assert websocket.receive_json() == {"event": "ping", "data": {"ok": True}}


@WebSocketGateway("/advanced-socket")
class AdvancedSocketGateway:
    def __init__(self, server: SocketIoServer):
        self.server = server

    async def on_connect(self, websocket):
        self.server.join(websocket, "all")

    @SubscribeMessage("broadcast")
    async def broadcast(self, data, websocket):
        await self.server.emit("notice", data)

    @SubscribeMessage("custom")
    async def custom(self, data, websocket):
        return {"event": "custom-result", "data": data}


@Module(gateways=[AdvancedSocketGateway])
class AdvancedSocketModule:
    pass


@WebSocketGateway("/global-socket")
class GlobalSocketGateway:
    def __init__(self, server: SocketIoServer):
        self.server = server

    @SubscribeMessage("broadcast")
    async def broadcast(self, data, websocket):
        await self.server.emit("global", data)


@Module(gateways=[GlobalSocketGateway])
class GlobalSocketModule:
    pass


def test_socket_io_server_broadcasts_to_roomless_connections():
    client = TestClient(FaNestFactory.create(GlobalSocketModule))

    with client.websocket_connect("/global-socket") as sender:
        with client.websocket_connect("/global-socket") as receiver:
            sender.send_json({"event": "broadcast", "data": {"text": "hi"}})
            assert sender.receive_json() == {"event": "global", "data": {"text": "hi"}}
            assert receiver.receive_json() == {"event": "global", "data": {"text": "hi"}}


def test_socket_io_server_broadcasts_to_all_connections_and_custom_response_event():
    client = TestClient(FaNestFactory.create(AdvancedSocketModule))

    with client.websocket_connect("/advanced-socket") as sender:
        with client.websocket_connect("/advanced-socket") as receiver:
            sender.send_json({"event": "broadcast", "data": {"text": "hi"}})
            assert sender.receive_json() == {"event": "notice", "data": {"text": "hi"}}
            assert receiver.receive_json() == {"event": "notice", "data": {"text": "hi"}}
            sender.send_json({"event": "custom", "data": {"value": 1}})
            assert sender.receive_json() == {"event": "custom-result", "data": {"value": 1}}


def test_websocket_gateway_reports_malformed_payloads():
    client = TestClient(FaNestFactory.create(AdvancedSocketModule))

    with client.websocket_connect("/advanced-socket") as websocket:
        websocket.send_text("{")
        assert websocket.receive_json()["event"] == "error"


@Catch(ValueError)
class BrokenWebSocketFilter:
    def catch(self, exc, context):
        raise RuntimeError("filter failed")


@WebSocketGateway("/broken-filter")
@UseFilters(BrokenWebSocketFilter)
class BrokenFilterGateway:
    @SubscribeMessage("fail")
    async def fail(self, data, websocket):
        raise ValueError("handler failed")

    @SubscribeMessage("echo")
    async def echo(self, data, websocket):
        return data


@Module(gateways=[BrokenFilterGateway])
class BrokenFilterModule:
    pass


def test_websocket_filter_exception_returns_error_without_closing_connection():
    client = TestClient(FaNestFactory.create(BrokenFilterModule))

    with client.websocket_connect("/broken-filter") as websocket:
        websocket.send_json({"event": "fail", "data": None})
        assert websocket.receive_json() == {"event": "error", "data": "filter failed"}
        websocket.send_json({"event": "echo", "data": "still-open"})
        assert websocket.receive_json() == {"event": "echo", "data": "still-open"}


async def async_ws_param_factory(data, context):
    return {"marker": data, "path": context.request.url.path}


AsyncSocketParam = create_param_decorator(async_ws_param_factory)


@WebSocketGateway("/async-param-socket")
class AsyncParamGateway:
    @SubscribeMessage("inspect")
    async def inspect_param(self, resolved=AsyncSocketParam("socket")):
        return resolved


@Module(gateways=[AsyncParamGateway])
class AsyncParamModule:
    pass


def test_websocket_gateway_awaits_async_custom_parameter_decorators():
    client = TestClient(FaNestFactory.create(AsyncParamModule))

    with client.websocket_connect("/async-param-socket") as websocket:
        websocket.send_json({"event": "inspect", "data": None})
        assert websocket.receive_json() == {
            "event": "inspect",
            "data": {"marker": "socket", "path": "/async-param-socket"},
        }


@WebSocketGateway("/disconnect-hook-socket")
class DisconnectHookGateway:
    disconnected = 0

    async def on_disconnect(self, websocket):
        type(self).disconnected += 1
        raise RuntimeError("cleanup failed")

    @SubscribeMessage("echo")
    async def echo(self, data):
        return data


@Module(gateways=[DisconnectHookGateway])
class DisconnectHookModule:
    pass


def test_websocket_disconnect_hook_exception_does_not_escape_teardown():
    DisconnectHookGateway.disconnected = 0
    client = TestClient(FaNestFactory.create(DisconnectHookModule))

    with client.websocket_connect("/disconnect-hook-socket") as websocket:
        websocket.send_json({"event": "echo", "data": "ok"})
        assert websocket.receive_json() == {"event": "echo", "data": "ok"}

    assert DisconnectHookGateway.disconnected == 1


class RecordingSocket:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.messages = []

    async def send_json(self, payload):
        if self.fail:
            raise RuntimeError("closed")
        self.messages.append(payload)


@pytest.mark.anyio
async def test_websocket_manager_prunes_failed_connections_during_broadcast():
    manager = WebSocketManager()
    healthy = RecordingSocket()
    broken = RecordingSocket(fail=True)
    manager.connect(healthy)
    manager.connect(broken)

    await manager.broadcast_all("notice", {"ok": True})

    assert healthy.messages == [{"event": "notice", "data": {"ok": True}}]
    assert manager.connections() == [healthy]
