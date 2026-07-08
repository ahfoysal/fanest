from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, UseGuards
from fanest.auth import AuthModule, CurrentUser, JwtAuthGuard, JwtService
from fanest.health import HealthModule


@Controller("profile")
class ProfileController:
    @UseGuards(JwtAuthGuard)
    @Get("/")
    async def profile(self, user: dict = CurrentUser()):
        return {"user": user}


@Module(
    imports=[
        AuthModule.for_root(secret="test-secret-value-with-enough-entropy"),
        HealthModule.register(),
    ],
    controllers=[ProfileController],
)
class AuthAppModule:
    pass


def test_jwt_auth_guard_and_current_user():
    app = FaNestFactory.create(AuthAppModule)
    client = TestClient(app)
    token = app.state.fanest_container.resolve(JwtService).sign({"sub": "123"})

    unauthorized = client.get("/profile")
    authorized = client.get("/profile", headers={"authorization": f"Bearer {token}"})

    assert unauthorized.status_code == 401
    assert authorized.json()["user"]["sub"] == "123"


def test_health_module_registers_health_endpoint():
    app = FaNestFactory.create(AuthAppModule)
    response = TestClient(app).get("/health")

    assert response.json() == {"status": "ok"}
