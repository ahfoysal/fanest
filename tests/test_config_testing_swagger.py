from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module
from fanest.config import ConfigModule, ConfigService
from fanest.swagger import ApiTags
from fanest.testing import TestingModule


@Injectable()
class MessageService:
    def __init__(self, config: ConfigService):
        self.config = config

    def message(self):
        return self.config.get("FANEST_MESSAGE", "real")


class MockMessageService:
    def message(self):
        return "mock"


@ApiTags("messages")
@Controller("messages")
class MessageController:
    def __init__(self, message_service: MessageService):
        self.message_service = message_service

    @Get("/")
    async def index(self):
        return {"message": self.message_service.message()}


@Module(
    imports=[ConfigModule.for_root(env_file=None)],
    controllers=[MessageController],
    providers=[MessageService],
)
class MessageModule:
    pass


def test_testing_module_provider_override():
    app = TestingModule.create(MessageModule).override_provider(
        MessageService, MockMessageService()
    ).compile()
    response = TestClient(app).get("/messages")

    assert response.json() == {"message": "mock"}


def test_testing_module_override_builder():
    app = TestingModule.create(MessageModule).override(MessageService).use_value(MockMessageService()).compile()

    assert TestClient(app).get("/messages").json() == {"message": "mock"}


def test_swagger_tags_are_registered():
    app = FaNestFactory.create(MessageModule)
    schema = TestClient(app).get("/openapi.json").json()

    operation = schema["paths"]["/messages"]["get"]
    assert operation["tags"] == ["messages"]
