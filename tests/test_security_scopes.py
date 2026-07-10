from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module
from fanest.auth import (
    AuthModule,
    CurrentSecurityScopes,
    JwtService,
    Public,
    Scopes,
    SecurityScopes,
    granted_scopes,
)
from fanest.swagger import DocumentBuilder, SwaggerModule


@Controller("items")
class ItemsController:
    @Scopes("items:read")
    @Get("/")
    async def index(self):
        return {"items": []}

    @Scopes("items:read", "items:write")
    @Get("manage")
    async def manage(self, scopes: SecurityScopes = CurrentSecurityScopes()):
        return {"required": scopes.scopes, "scope_str": scopes.scope_str}

    @Get("open")
    async def open(self):
        return {"open": True}

    @Public()
    @Get("public")
    async def public(self):
        return {"public": True}


@Scopes("admin:all")
@Controller("admin")
class AdminController:
    @Get("/")
    async def index(self):
        return {"admin": True}


@Module(
    imports=[
        AuthModule.for_root(
            secret="scopes-secret-value-with-enough-entropy",
            global_guard=True,
        )
    ],
    controllers=[ItemsController, AdminController],
)
class ScopesAppModule:
    pass


def _client_and_signer():
    app = FaNestFactory.create(ScopesAppModule)
    jwt_service = app.state.fanest_container.resolve(JwtService)

    def bearer(payload):
        return {"authorization": f"Bearer {jwt_service.sign(payload)}"}

    return TestClient(app), bearer


def test_scopes_guard_enforces_all_required_scopes():
    client, bearer = _client_and_signer()

    # Space-delimited standard `scope` claim.
    assert client.get("/items", headers=bearer({"sub": "1", "scope": "items:read"})).status_code == 200
    # Missing scope -> 403.
    response = client.get("/items", headers=bearer({"sub": "1", "scope": "profile"}))
    assert response.status_code == 403
    assert "items:read" in response.json()["detail"]
    # ALL scopes are required, not any.
    assert (
        client.get("/items/manage", headers=bearer({"sub": "1", "scope": "items:read"})).status_code
        == 403
    )
    assert (
        client.get(
            "/items/manage", headers=bearer({"sub": "1", "scope": "items:read items:write"})
        ).status_code
        == 200
    )
    # List-valued claim also accepted.
    assert (
        client.get("/items", headers=bearer({"sub": "1", "scopes": ["items:read"]})).status_code
        == 200
    )


def test_scopes_guard_controller_level_public_and_unscoped_routes():
    client, bearer = _client_and_signer()

    # Controller-level scopes apply to all handlers.
    assert client.get("/admin", headers=bearer({"sub": "1", "scope": "profile"})).status_code == 403
    assert client.get("/admin", headers=bearer({"sub": "1", "scope": "admin:all"})).status_code == 200
    # Routes without @Scopes only need authentication.
    assert client.get("/items/open", headers=bearer({"sub": "1"})).status_code == 200
    # @Public bypasses scope checks entirely.
    assert client.get("/items/public").json() == {"public": True}
    # No token at all on a scoped route -> 401 from the auth guard.
    assert client.get("/items").status_code == 401


def test_current_security_scopes_parameter_reports_route_requirements():
    client, bearer = _client_and_signer()

    body = client.get(
        "/items/manage", headers=bearer({"sub": "1", "scope": "items:read items:write"})
    ).json()

    assert body == {
        "required": ["items:read", "items:write"],
        "scope_str": "items:read items:write",
    }


def test_scopes_decorator_wires_openapi_security_and_document_builder_flows():
    app = FaNestFactory.create(ScopesAppModule)
    config = (
        DocumentBuilder()
        .set_title("Scoped API")
        .add_oauth2(
            token_url="https://auth.example.com/token",
            scopes={"items:read": "Read items", "items:write": "Write items"},
        )
        .build()
    )
    document = SwaggerModule.create_document(app, config)
    SwaggerModule.setup("/docs", app, document)
    schema = TestClient(app).get("/docs/openapi.json").json()

    assert schema["components"]["securitySchemes"]["oauth2"]["type"] == "oauth2"
    operation = schema["paths"]["/items"]["get"]
    assert {"oauth2": ["items:read"]} in operation["security"]
    manage_operation = schema["paths"]["/items/manage"]["get"]
    assert {"oauth2": ["items:read", "items:write"]} in manage_operation["security"]
    admin_operation = schema["paths"]["/admin"]["get"]
    assert {"oauth2": ["admin:all"]} in admin_operation["security"]


def test_granted_scopes_claim_extraction():
    assert granted_scopes({"scope": "a b c"}) == ["a", "b", "c"]
    assert granted_scopes({"scopes": ["a", "b"]}) == ["a", "b"]
    assert granted_scopes({"scp": "x y"}) == ["x", "y"]
    assert granted_scopes({"sub": "1"}) == []
    assert granted_scopes(None) == []
