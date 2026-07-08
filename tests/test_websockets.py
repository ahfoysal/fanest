from fastapi.testclient import TestClient

from fanest import (
    FaNestFactory,
    Module,
    SubscribeMessage,
    UseGuards,
    UsePipes,
    WebSocketGateway,
    WebSocketManager,
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

    with client.websocket_connect("/secure-chat?token=bad") as websocket:
        websocket.send_json({"event": "shout", "data": "hello"})
        assert websocket.receive_json()["event"] == "error"

    with client.websocket_connect("/secure-chat?token=ok") as websocket:
        websocket.send_json({"event": "shout", "data": "hello"})
        assert websocket.receive_json() == {
            "event": "shout",
            "data": {"message": "HELLO"},
        }
