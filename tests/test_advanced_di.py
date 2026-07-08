from fastapi.testclient import TestClient

from fanest import (
    Controller,
    FaNestFactory,
    Get,
    Inject,
    Module,
    Optional,
    token,
    use_class,
    use_existing,
    use_factory,
    use_value,
)

CONFIG = token("CONFIG")
MESSAGE = token("MESSAGE")
ALIAS = token("ALIAS")
OPTIONAL = token("OPTIONAL")


class BaseFormatter:
    def format(self, value: str) -> str:
        return value


class UpperFormatter(BaseFormatter):
    def format(self, value: str) -> str:
        return value.upper()


class MessageService:
    def __init__(
        self,
        message: str = Inject(MESSAGE),
        alias: str = Inject(ALIAS),
        optional: str | None = Optional(OPTIONAL),
        formatter: BaseFormatter = Inject(BaseFormatter),
    ):
        self.message = message
        self.alias = alias
        self.optional = optional
        self.formatter = formatter

    def get(self):
        return {
            "message": self.formatter.format(self.message),
            "alias": self.alias,
            "optional": self.optional,
        }


@Controller("messages")
class MessageController:
    def __init__(self, service: MessageService):
        self.service = service

    @Get("/")
    async def index(self):
        return self.service.get()


@Module(
    controllers=[MessageController],
    providers=[
        MessageService,
        use_value(CONFIG, {"message": "hello"}),
        use_factory(MESSAGE, lambda config: config["message"], inject=[CONFIG]),
        use_existing(ALIAS, MESSAGE),
        use_class(BaseFormatter, UpperFormatter),
    ],
)
class AdvancedDiModule:
    pass


def test_class_value_factory_existing_and_optional_providers():
    client = TestClient(FaNestFactory.create(AdvancedDiModule))

    assert client.get("/messages").json() == {
        "message": "HELLO",
        "alias": "hello",
        "optional": None,
    }
