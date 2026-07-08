from fastapi.testclient import TestClient
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
