from fastapi.testclient import TestClient
import pytest

from fanest import Controller, FaNestFactory, Get, Injectable, Module
from fanest.config import ConfigModule, ConfigService
from fanest.swagger import ApiTags
from fanest.testing import TestingModule, create_testing_module


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


def test_testing_module_helpers():
    module = create_testing_module(MessageModule).override(MessageService).use_value(MockMessageService())
    client = module.create_test_client()

    assert client.get("/messages").json() == {"message": "mock"}
    assert module.get(MessageService).message() == "mock"
    module.close()
    assert module._app is None


@pytest.mark.anyio
async def test_testing_module_compile_async():
    app = await TestingModule.create(MessageModule).compile_async()

    assert TestClient(app).get("/messages").json()["message"] == "real"


def test_swagger_tags_are_registered():
    app = FaNestFactory.create(MessageModule)
    schema = TestClient(app).get("/openapi.json").json()

    operation = schema["paths"]["/messages"]["get"]
    assert operation["tags"] == ["messages"]
