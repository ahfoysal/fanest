from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, UseGuards
from fanest.auth import AuthModule, CurrentUser, JwtAuthGuard, JwtService
from fanest.config import ConfigModule, ConfigService
from fanest.sqlalchemy import SqlAlchemyModule, SqlAlchemyService


@Controller("global-config")
class GlobalConfigController:
    def __init__(self, config: ConfigService):
        self.config = config

    @Get("/")
    async def index(self):
        return {"name": self.config.get("APP_NAME")}


@Module(controllers=[GlobalConfigController])
class GlobalConfigFeatureModule:
    pass


@Module(
    imports=[
        ConfigModule.for_root(values={"APP_NAME": "FaNest"}, is_global=True),
        GlobalConfigFeatureModule,
    ]
)
class GlobalConfigAppModule:
    pass


def test_dynamic_module_is_global_exports_providers_to_other_modules():
    response = TestClient(FaNestFactory.create(GlobalConfigAppModule)).get("/global-config")

    assert response.json() == {"name": "FaNest"}


@Controller("global-auth")
@UseGuards(JwtAuthGuard)
class GlobalAuthController:
    @Get("/")
    async def profile(self, user: dict = CurrentUser()):
        return {"sub": user["sub"]}


@Module(controllers=[GlobalAuthController])
class GlobalAuthFeatureModule:
    pass


@Module(
    imports=[
        AuthModule.for_root(secret="global-secret-value-with-enough-entropy", is_global=True),
        GlobalAuthFeatureModule,
    ]
)
class GlobalAuthAppModule:
    pass


def test_global_auth_and_state_params_do_not_leak_into_openapi():
    app = FaNestFactory.create(GlobalAuthAppModule)
    client = TestClient(app)
    token = app.state.fanest_container.resolve(JwtService).sign({"sub": "42"})

    assert client.get("/global-auth", headers={"authorization": f"Bearer {token}"}).json() == {
        "sub": "42"
    }
    operation = app.openapi()["paths"]["/global-auth"]["get"]
    assert "parameters" not in operation
    assert "requestBody" not in operation


@Controller("global-db")
class GlobalDbController:
    def __init__(self, db: SqlAlchemyService):
        self.db = db

    @Get("/")
    async def index(self):
        return {"engine": self.db.engine.url.get_backend_name()}


@Module(controllers=[GlobalDbController])
class GlobalDbFeatureModule:
    pass


@Module(
    imports=[
        SqlAlchemyModule.for_root(database_url="sqlite+aiosqlite:///:memory:", is_global=True),
        GlobalDbFeatureModule,
    ]
)
class GlobalDbAppModule:
    pass


def test_global_database_module_exports_service_to_feature_module():
    app = FaNestFactory.create(GlobalDbAppModule)
    response = TestClient(app).get("/global-db")

    assert response.json() == {"engine": "sqlite"}
