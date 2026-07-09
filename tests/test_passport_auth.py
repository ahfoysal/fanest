from fastapi.testclient import TestClient
import pytest

from fanest import Controller, FaNestFactory, Get, Module, UseGuards
from fanest.auth import AuthGuard, CurrentUser, PassportModule, PassportStrategy, Public


class ApiKeyStrategy(PassportStrategy):
    name = "api-key"

    def authenticate(self, context):
        if context.request.headers.get("x-api-key") == "secret":
            return {"sub": "api-user"}
        return None


class AsyncApiKeyStrategy(PassportStrategy):
    name = "async-api-key"

    async def authenticate(self, context):
        if context.request.headers.get("x-api-key") == "async-secret":
            return {"sub": "async-user"}
        return None


@Controller("passport")
@UseGuards(AuthGuard("api-key"))
class PassportController:
    @Get("/")
    async def index(self, user = CurrentUser()):
        return {"user": user}

    @Public()
    @Get("/public")
    async def public(self):
        return {"public": True}


@Module(
    imports=[PassportModule.register(ApiKeyStrategy)],
    controllers=[PassportController],
)
class PassportAppModule:
    pass


@Controller("passport-default")
@UseGuards(AuthGuard(" "))
class DefaultPassportController:
    @Get("/")
    async def index(self, user=CurrentUser()):
        return {"user": user}


@Module(
    imports=[PassportModule.register(ApiKeyStrategy, default_strategy="api-key")],
    controllers=[DefaultPassportController],
)
class DefaultPassportModule:
    pass


@Controller("passport-async")
@UseGuards(AuthGuard("async-api-key"))
class AsyncPassportController:
    @Get("/")
    async def index(self, user=CurrentUser()):
        return {"user": user}


async def load_passport_options():
    return {
        "default_strategy": "async-api-key",
        "strategies": [AsyncApiKeyStrategy],
    }


@Module(
    imports=[PassportModule.register_async(use_factory=load_passport_options)],
    controllers=[AsyncPassportController],
)
class AsyncPassportModule:
    pass


def test_passport_strategy_guard_authenticates_requests():
    with TestClient(FaNestFactory.create(PassportAppModule)) as client:
        assert client.get("/passport").status_code == 401
        assert client.get("/passport", headers={"x-api-key": "secret"}).json() == {
            "user": {"sub": "api-user"}
        }
        assert client.get("/passport/public").json() == {"public": True}


def test_passport_guard_blank_strategy_uses_default_strategy():
    with TestClient(FaNestFactory.create(DefaultPassportModule)) as client:
        assert client.get("/passport-default").status_code == 401
        assert client.get("/passport-default", headers={"x-api-key": "secret"}).json() == {
            "user": {"sub": "api-user"}
        }


def test_passport_register_async_supports_async_strategy_classes():
    with TestClient(FaNestFactory.create(AsyncPassportModule)) as client:
        assert client.get("/passport-async").status_code == 401
        assert client.get("/passport-async", headers={"x-api-key": "async-secret"}).json() == {
            "user": {"sub": "async-user"}
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


class DuplicateApiKeyStrategy(PassportStrategy):
    name = "api-key"


class EmptyNameStrategy(PassportStrategy):
    name = ""


class ExplodingStrategy(PassportStrategy):
    name = "exploding"

    def authenticate(self, context):
        raise RuntimeError("boom")


@Controller("exploding-passport")
@UseGuards(AuthGuard("exploding"))
class ExplodingPassportController:
    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(imports=[PassportModule.register(ExplodingStrategy)], controllers=[ExplodingPassportController])
class ExplodingPassportModule:
    pass


def test_passport_module_rejects_duplicate_and_empty_strategy_names():
    with pytest.raises(ValueError, match="already registered"):
        PassportModule.register(ApiKeyStrategy, DuplicateApiKeyStrategy)

    with pytest.raises(ValueError, match="non-empty name"):
        PassportModule.register(EmptyNameStrategy)


def test_passport_strategy_exceptions_do_not_authenticate():
    with pytest.raises(RuntimeError, match="boom"):
        with TestClient(FaNestFactory.create(ExplodingPassportModule)) as client:
            client.get("/exploding-passport")
