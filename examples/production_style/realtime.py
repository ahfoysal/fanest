from fanest import SubscribeMessage, WebSocketGateway
from fanest.websockets import WebSocketManager


@WebSocketGateway("/ws/users")
class UsersGateway:
    def __init__(self, manager: WebSocketManager):
        self.manager = manager

    async def on_connect(self, websocket):
        self.manager.join("users", websocket)

    @SubscribeMessage("ping")
    async def ping(self, data, websocket):
        return {"pong": data}

    @SubscribeMessage("user.typing")
    async def typing(self, data, websocket):
        await self.manager.broadcast("users", "user.typing", data, exclude=websocket)
        return {"sent": True}
