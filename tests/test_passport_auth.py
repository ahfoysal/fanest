from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, UseGuards
from fanest.auth import AuthGuard, CurrentUser, PassportModule, PassportStrategy


class ApiKeyStrategy(PassportStrategy):
    name = "api-key"

    def authenticate(self, context):
        if context.request.headers.get("x-api-key") == "secret":
            return {"sub": "api-user"}
        return None


@Controller("passport")
@UseGuards(AuthGuard("api-key"))
class PassportController:
    @Get("/")
    async def index(self, user: dict = CurrentUser()):
        return {"user": user}


@Module(
    imports=[PassportModule.register(ApiKeyStrategy)],
    controllers=[PassportController],
)
class PassportAppModule:
    pass


def test_passport_strategy_guard_authenticates_requests():
    with TestClient(FaNestFactory.create(PassportAppModule)) as client:
        assert client.get("/passport").status_code == 403
        assert client.get("/passport", headers={"x-api-key": "secret"}).json() == {
            "user": {"sub": "api-user"}
        }
