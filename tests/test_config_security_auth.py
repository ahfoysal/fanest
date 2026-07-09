import pytest
import jwt
from fastapi.testclient import TestClient
from pydantic import BaseModel
from typing import Any, cast

from fanest import Controller, FaNestFactory, Get, Module, UseGuards
from fanest.auth import AuthModule, JwtAuthGuard, JwtService
from fanest.config import ConfigModule, ConfigService
from fanest.security import CsrfModule, HelmetModule, PasswordHasher, UnsupportedSecurityFeatureError


@Controller("secure")
@UseGuards(JwtAuthGuard)
class SecureController:
    @Get("/")
    async def index(self):
        return {"ok": True}


def test_config_env_file_does_not_override_process_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_MODE=file\nAPP_NAME=file-name\n", encoding="utf-8")
    monkeypatch.setenv("APP_MODE", "process")

    @Module(
        imports=[
            ConfigModule.for_root(
                env_file=str(env_file),
                values={"APP_NAME": "explicit-name"},
            )
        ]
    )
    class ConfigPrecedenceModule:
        pass

    config = FaNestFactory.create(ConfigPrecedenceModule).state.fanest_container.resolve(ConfigService)

    assert config.get("APP_MODE") == "process"
    assert config.get("APP_NAME") == "explicit-name"


def test_cors_rejects_wildcard_origins_with_credentials():
    @Module()
    class CorsModule:
        pass

    with pytest.raises(ValueError, match="allow_credentials=True"):
        FaNestFactory.create(
            CorsModule,
            cors={"allow_origins": ["*"], "allow_credentials": True},
        )

    with pytest.raises(ValueError, match="allow_credentials must be a boolean"):
        FaNestFactory.create(CorsModule, cors={"allow_origins": ["https://app.test"], "allow_credentials": "yes"})

    with pytest.raises(ValueError, match="allow_origins cannot contain empty"):
        FaNestFactory.create(CorsModule, cors={"allow_origins": ["https://app.test", ""]})


def test_cors_normalizes_single_string_origin_without_wildcarding():
    @Module()
    class CorsStringModule:
        pass

    client = TestClient(
        FaNestFactory.create(
            CorsStringModule,
            cors={
                "allow_origins": "https://app.test",
                "allow_methods": "GET",
                "allow_headers": "authorization",
            },
        )
    )

    allowed = client.options(
        "/missing",
        headers={
            "origin": "https://app.test",
            "access-control-request-method": "GET",
            "access-control-request-headers": "authorization",
        },
    )
    blocked = client.options(
        "/missing",
        headers={
            "origin": "https://evil.test",
            "access-control-request-method": "GET",
        },
    )

    assert allowed.headers["access-control-allow-origin"] == "https://app.test"
    assert "access-control-allow-origin" not in blocked.headers


def test_auth_module_rejects_empty_and_unsigned_jwt_configuration():
    with pytest.raises(ValueError, match="non-empty JWT secret"):
        AuthModule.for_root(secret="")

    with pytest.raises(ValueError, match="unsigned JWT algorithms"):
        AuthModule.for_root(secret="valid-secret-value-with-enough-entropy", algorithm="none")

    with pytest.raises(ValueError, match="required_claims"):
        AuthModule.for_root(
            secret="valid-secret-value-with-enough-entropy",
            required_claims="sub",  # type: ignore[arg-type]
        )


def test_jwt_auth_guard_rejects_empty_bearer_token():
    @Module(
        imports=[AuthModule.for_root(secret="guard-secret-value-with-enough-entropy")],
        controllers=[SecureController],
    )
    class SecureModule:
        pass

    response = TestClient(FaNestFactory.create(SecureModule)).get(
        "/secure",
        headers={"authorization": "Bearer    "},
    )

    assert response.status_code == 401


@Module(
    imports=[
        AuthModule.for_root(
            secret="claims-secret-value-with-enough-entropy",
            issuer="fanest",
            audience="api",
            required_claims=["sub", "iss", "aud"],
        )
    ]
)
class JwtClaimsModule:
    pass


def test_jwt_service_validates_issuer_audience_and_required_claims():
    app = FaNestFactory.create(JwtClaimsModule)
    service = app.state.fanest_container.resolve(JwtService)

    token_value = service.sign({"sub": 123})

    assert service.verify(token_value)["sub"] == "123"

    missing_claims = service.sign(
        {"sub": "123"},
        issuer=None,
        audience=None,
        expires_in_seconds=None,
    )
    with pytest.raises(jwt.MissingRequiredClaimError):
        service.verify(missing_claims)

    wrong_audience = service.sign({"sub": "123"}, audience="other")
    with pytest.raises(jwt.InvalidAudienceError):
        service.verify(wrong_audience)


def test_jwt_service_rejects_unsafe_per_call_algorithm_overrides():
    app = FaNestFactory.create(JwtClaimsModule)
    service = app.state.fanest_container.resolve(JwtService)
    token_value = service.sign({"sub": "123"})

    with pytest.raises(ValueError, match="unsigned"):
        service.sign({"sub": "123"}, algorithm="none")

    with pytest.raises(ValueError, match="unsigned"):
        service.verify(token_value, algorithms=["none"])

    with pytest.raises(ValueError, match="cannot be empty"):
        service.verify(token_value, algorithms=[])


def test_config_nested_lookup_strict_bool_and_expanded_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setenv("HOST_FROM_ENV", "db.internal")
    env_file.write_text(
        "DATABASE_HOST=$HOST_FROM_ENV\nFEATURE_FLAG=not-bool\n",
        encoding="utf-8",
    )

    @Module(
        imports=[
            ConfigModule.for_root(
                env_file=str(env_file),
                expand_variables=True,
                values={"database": {"port": "5432"}},
            )
        ]
    )
    class NestedConfigModule:
        pass

    config = FaNestFactory.create(NestedConfigModule).state.fanest_container.resolve(ConfigService)

    assert config.get("DATABASE_HOST") == "db.internal"
    assert config.get("database.port", cast=int) == 5432
    with pytest.raises(ValueError, match="Cannot cast"):
        config.get("FEATURE_FLAG", cast=bool)


def test_config_advanced_options_ignore_sources_and_load_namespaces(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_MODE=file\n", encoding="utf-8")
    monkeypatch.setenv("APP_MODE", "process")
    monkeypatch.setenv("SECRET_FROM_ENV", "hidden")

    @Module(
        imports=[
            ConfigModule.for_root(
                env_file=str(env_file),
                ignore_env_file=True,
                ignore_env_vars=True,
                load=[lambda: {"database": {"host": "db.internal", "port": "5432"}}],
                values={"APP_MODE": "explicit"},
            )
        ]
    )
    class AdvancedConfigModule:
        pass

    config = FaNestFactory.create(AdvancedConfigModule).state.fanest_container.resolve(ConfigService)
    database = config.namespace("database")

    assert config.get("APP_MODE") == "explicit"
    assert config.has("SECRET_FROM_ENV") is False
    assert database.get("host") == "db.internal"
    assert database.get("port", cast=int) == 5432
    with pytest.raises(TypeError, match="not an object"):
        config.namespace("APP_MODE")


class AsyncConfigSchema(BaseModel):
    app_name: str
    database: dict[str, int]


async def load_async_config():
    return {"app_name": "async-fanest", "database": {"port": "5432"}}


def test_config_module_for_root_async_is_real_class_api_and_validates_schema():
    @Module(
        imports=[
            ConfigModule.for_root_async(
                use_factory=load_async_config,
                env_file=None,
                schema=AsyncConfigSchema,
            )
        ]
    )
    class AsyncConfigRegressionModule:
        pass

    with TestClient(FaNestFactory.create(AsyncConfigRegressionModule)) as client:
        config = cast(Any, client.app).state.fanest_container.resolve(ConfigService)

    assert config.get("app_name") == "async-fanest"
    assert config.get("database.port") == 5432
    assert config.get_required("database.port", cast=int) == 5432


@Controller("security-headers")
class SecurityHeadersController:
    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(
    imports=[HelmetModule.for_root(headers={"x-frame-options": "SAMEORIGIN"})],
    controllers=[SecurityHeadersController],
)
class SecurityHeadersModule:
    pass


def test_security_headers_merge_defaults_and_reject_injection():
    response = TestClient(FaNestFactory.create(SecurityHeadersModule)).get("/security-headers")

    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-permitted-cross-domain-policies"] == "none"

    with pytest.raises(ValueError, match="newline"):
        HelmetModule.for_root(headers={"x-test": "ok\r\nset-cookie: stolen=true"})


def test_csrf_module_fails_explicitly_until_a_real_middleware_is_supported():
    with pytest.raises(UnsupportedSecurityFeatureError, match="CSRF protection is not built into FaNest"):
        CsrfModule.for_root()


def test_password_hasher_uses_salted_pbkdf2_and_constant_time_verify():
    hasher = PasswordHasher(iterations=100_000)

    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")

    assert first != second
    assert hasher.verify("correct horse battery staple", first)
    assert not hasher.verify("wrong password", first)
    assert not hasher.verify("correct horse battery staple", "not-a-valid-hash")
    assert not hasher.verify("correct horse battery staple", "pbkdf2_not-real$100000$c2FsdA$ZGlnZXN0")

    with pytest.raises(ValueError, match="iterations"):
        PasswordHasher(iterations=99_999)


    with pytest.raises(ValueError, match="names cannot be empty"):
        HelmetModule.for_root(headers={"   ": "bad"})


@Module(
    imports=[HelmetModule.for_root(headers={"x-frame-options": None})],
    controllers=[SecurityHeadersController],
)
class DisabledSecurityHeaderModule:
    pass


def test_security_headers_can_disable_a_default_safely():
    response = TestClient(FaNestFactory.create(DisabledSecurityHeaderModule)).get("/security-headers")

    assert "x-frame-options" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
