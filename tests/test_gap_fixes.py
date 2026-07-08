from pydantic import BaseModel

import pytest
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module, Post, UseGuards, UseInterceptors
from fanest.auth import AuthModule, JwtAuthGuard, Public
from fanest.cache import CacheEvict, CacheInterceptor, CacheKey, CacheModule, CacheService, CacheTTL
from fanest.config import ConfigModule, ConfigService
from fanest.sqlalchemy import SqlAlchemyModule, SqlAlchemyService
from fanest.testing import TestingModule


@Controller("public")
@UseGuards(JwtAuthGuard)
class PublicController:
    @Public()
    @Get("/")
    async def index(self):
        return {"public": True}


@Module(
    imports=[AuthModule.for_root(secret="public-secret-value-with-enough-entropy")],
    controllers=[PublicController],
)
class PublicModule:
    pass


def test_public_decorator_skips_global_auth_guard():
    response = TestClient(FaNestFactory.create(PublicModule)).get("/public")

    assert response.status_code == 200
    assert response.json() == {"public": True}


@Injectable(scope="request")
class RequestScopedValue:
    pass


@Injectable()
class SingletonValue:
    pass


@Module(providers=[RequestScopedValue, SingletonValue])
class TestingGetModule:
    pass


def test_testing_module_get_and_resolve():
    module = TestingModule.create(TestingGetModule)

    assert module.get(SingletonValue) is module.get(SingletonValue)
    assert module.resolve(RequestScopedValue) is not module.resolve(RequestScopedValue)


class AppSettings(BaseModel):
    APP_PORT: int
    DEBUG: bool = False


@Module(imports=[ConfigModule.for_root(values={"APP_PORT": "8080", "DEBUG": "true"}, schema=AppSettings)])
class ConfigAppModule:
    pass


def test_config_schema_and_typed_get():
    app = FaNestFactory.create(ConfigAppModule)
    config = app.state.fanest_container.resolve(ConfigService)

    assert config.get("APP_PORT", cast=int) == 8080
    assert config.get("DEBUG", cast=bool) is True


@Controller("cache-gap")
@UseInterceptors(CacheInterceptor)
class CacheGapController:
    calls = 0

    @CacheKey("cache-gap:index")
    @CacheTTL(60)
    @Get("/")
    async def index(self):
        type(self).calls += 1
        return {"calls": type(self).calls}

    @CacheEvict("cache-gap:index")
    @Post("/reset")
    async def reset(self):
        return {"reset": True}


@Module(imports=[CacheModule.register()], controllers=[CacheGapController])
class CacheGapModule:
    pass


def test_cache_key_and_evict():
    CacheGapController.calls = 0
    app = FaNestFactory.create(CacheGapModule)
    app.state.fanest_container.resolve(CacheService).clear()
    client = TestClient(app)

    assert client.get("/cache-gap").json() == {"calls": 1}
    assert client.get("/cache-gap").json() == {"calls": 1}
    assert client.post("/cache-gap/reset").json() == {"reset": True}
    assert client.get("/cache-gap").json() == {"calls": 2}


@Module(imports=[SqlAlchemyModule.for_root(database_url="sqlite+aiosqlite:///:memory:")])
class DbModule:
    pass


@pytest.mark.anyio
async def test_sqlalchemy_transaction_context():
    app = FaNestFactory.create(DbModule)
    db = app.state.fanest_container.resolve(SqlAlchemyService)

    async with db.transaction() as session:
        assert session.in_transaction()
