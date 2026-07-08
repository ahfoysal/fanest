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
        assert client.get("/passport").status_code == 401
        assert client.get("/passport", headers={"x-api-key": "secret"}).json() == {
            "user": {"sub": "api-user"}
        }


@Controller("missing-passport")
@UseGuards(AuthGuard("missing"))
class MissingPassportController:
    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(imports=[PassportModule.register()], controllers=[MissingPassportController])
class MissingPassportModule:
    pass


def test_passport_missing_strategy_returns_unauthorized():
    with TestClient(FaNestFactory.create(MissingPassportModule)) as client:
        assert client.get("/missing-passport").status_code == 401
