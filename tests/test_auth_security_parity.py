import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace

from fanest import Controller, FaNestFactory, ForbiddenException, Get, Module, Reflector
from fanest.auth import AuthModule, CurrentUser, JwtModule, JwtService, Public, Roles, RolesGuard


@Controller("global-jwt")
class GlobalJwtController:
    @Get("/")
    async def profile(self, user: dict = CurrentUser()):
        return {"sub": user["sub"]}

    @Public()
    @Get("/public")
    async def public(self):
        return {"public": True}


@Module(
    imports=[AuthModule.for_root(secret="global-jwt-secret-value-with-enough-entropy", global_guard=True)],
    controllers=[GlobalJwtController],
)
class GlobalJwtModule:
    pass


def test_auth_module_can_install_global_jwt_guard_with_public_bypass():
    app = FaNestFactory.create(GlobalJwtModule)
    client = TestClient(app)
    token = app.state.fanest_container.resolve(JwtService).sign({"sub": "123"})

    assert client.get("/global-jwt").status_code == 401
    assert client.get("/global-jwt", headers={"authorization": f"Bearer {token}"}).json() == {
        "sub": "123"
    }
    assert client.get("/global-jwt/public").json() == {"public": True}


@Roles("admin")
@Controller("roles")
class RolesController:
    @Get("/")
    async def index(self):
        return {"ok": True}

    @Roles("user")
    @Get("/method")
    async def method(self):
        return {"ok": True}


def test_roles_guard_reads_class_roles_and_method_override():
    guard = RolesGuard()
    controller = RolesController()

    context = SimpleNamespace(
        request=SimpleNamespace(state=SimpleNamespace(user={"roles": ["admin"]})),
        controller=controller,
        handler=controller.index,
    )

    assert guard.can_activate(context)
    context.handler = controller.method
    with pytest.raises(ForbiddenException):
        guard.can_activate(context)


def test_auth_decorators_are_reflector_visible_and_jwt_module_alias_works():
    reflector = Reflector()

    assert reflector.get("roles", RolesController) == ["admin"]
    assert reflector.get("roles", RolesController.method) == ["user"]
    assert reflector.get("is_public", GlobalJwtController.public) is True
    assert JwtModule is AuthModule
