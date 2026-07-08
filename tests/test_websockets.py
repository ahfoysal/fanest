from fastapi.testclient import TestClient

from fanest import FaNestFactory, Module, SubscribeMessage, WebSocketGateway


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
