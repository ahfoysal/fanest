from typing import Any, cast

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from fanest import BadRequestException, Controller, FaNestFactory, Get, Module, UseGuards
from fanest.auth import AuthGuard, JwtService, PassportModule, PassportStrategy
from fanest.common import pydantic_compat
from fanest.common.pipes import ValidationPipe
from fanest.config import ConfigModule, ConfigService
from fanest.security import HelmetModule, SecurityHeadersMiddleware


def test_jwt_service_enforces_edge_claims_and_clock_skew() -> None:
    service = JwtService(
        {
            "secret": "edge-claims-secret-value-with-enough-entropy",
            "algorithm": "HS256",
            "expires_in_seconds": 60,
            "issuer": "fanest",
            "audience": "api",
            "leeway": 0,
            "required_claims": ["sub", "iss", "aud"],
        }
    )

    expired = service.sign({"sub": "user-1"}, expires_in_seconds=-1)
    with pytest.raises(jwt.ExpiredSignatureError):
        service.verify(expired)

    not_before = service.sign({"sub": "user-1"}, not_before_seconds=20)
    with pytest.raises(jwt.ImmatureSignatureError):
        service.verify(not_before)
    assert service.verify(not_before, leeway=30)["sub"] == "user-1"

    missing_required_nbf = service.sign({"sub": "user-1"})
    with pytest.raises(jwt.MissingRequiredClaimError):
        service.verify(missing_required_nbf, required_claims=["sub", "iss", "aud", "nbf"])


class FalsyStrategy(PassportStrategy):
    name = "falsy"

    def authenticate(self, context: Any) -> Any:
        return {}


@Controller("passport-failure")
@UseGuards(AuthGuard("falsy"))
class PassportFailureController:
    @Get("/")
    async def index(self) -> dict[str, bool]:
        return {"ok": True}


@Module(imports=[PassportModule.register(FalsyStrategy)], controllers=[PassportFailureController])
class PassportFailureModule:
    pass


async def load_invalid_passport_options() -> dict[str, Any]:
    return {"strategies": [object()]}


@Module(imports=[PassportModule.register_async(use_factory=load_invalid_passport_options)])
class InvalidAsyncPassportModule:
    pass


def test_passport_strategy_failures_remain_unauthorized_or_rejected() -> None:
    with TestClient(FaNestFactory.create(PassportFailureModule)) as client:
        assert client.get("/passport-failure").status_code == 401

    with pytest.raises(ValueError, match="Passport strategies"):
        with TestClient(FaNestFactory.create(InvalidAsyncPassportModule)):
            pass


class NestedAsyncConfigSchema(BaseModel):
    APP_URL: str
    service: dict[str, Any]


def test_config_async_env_precedence_and_nested_lookup(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_URL=https://file.test\n", encoding="utf-8")
    monkeypatch.setenv("APP_URL", "https://process.test")

    async def load_config() -> dict[str, Any]:
        return {
            "APP_URL": "https://factory.test",
            "service": {"enabled": "true", "ports": ["8000"]},
        }

    @Module(
        imports=[
            ConfigModule.for_root_async(
                use_factory=load_config,
                env_file=str(env_file),
                schema=NestedAsyncConfigSchema,
            )
        ]
    )
    class NestedAsyncConfigModule:
        pass

    with TestClient(FaNestFactory.create(NestedAsyncConfigModule)) as client:
        config = cast(Any, client.app).state.fanest_container.resolve(ConfigService)

    assert config.get("APP_URL") == "https://factory.test"
    assert config.get("service.enabled", cast=bool) is True
    assert config.get("service.ports.0", cast=int) == 8000
    assert config.has("service.ports.1") is False


async def response_with_security_header(request: Any) -> PlainTextResponse:
    return PlainTextResponse("ok", headers={"x-frame-options": "SAMEORIGIN"})


def test_security_headers_are_strict_and_do_not_overwrite_existing_values() -> None:
    app = Starlette(routes=[Route("/", response_with_security_header)])
    app.add_middleware(SecurityHeadersMiddleware)
    response = TestClient(app).get("/")

    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"

    with pytest.raises(ValueError, match="newline"):
        HelmetModule.for_root(headers={"x-bad\r\nname": "value"})


def test_cors_rejects_invalid_strict_collections() -> None:
    @Module()
    class CorsStrictModule:
        pass

    with pytest.raises(ValueError, match="allow_headers must be a string or a list"):
        FaNestFactory.create(
            CorsStrictModule,
            cors={"allow_origins": ["https://app.test"], "allow_headers": object()},
        )

    with pytest.raises(ValueError, match="allow_methods cannot contain empty"):
        FaNestFactory.create(
            CorsStrictModule,
            cors={"allow_origins": ["https://app.test"], "allow_methods": ["GET", " "]},
        )


class ValidationShapeDto(BaseModel):
    age: int


def test_validation_pipe_error_shape_is_stable() -> None:
    with pytest.raises(BadRequestException) as exc_info:
        ValidationPipe().transform(
            {"age": "not-an-int"},
            {"annotation": ValidationShapeDto, "name": "body"},
        )

    error = cast(list[dict[str, Any]], exc_info.value.detail)[0]
    assert error["loc"] == ("age",)
    assert error["type"]
    assert error["msg"]


class LegacyPydanticModel:
    __fields__ = {"name": object()}

    def __init__(self, name: str) -> None:
        self.name = name

    @classmethod
    def parse_obj(cls, value: dict[str, Any]) -> "LegacyPydanticModel":
        return cls(str(value["name"]))

    def dict(self) -> dict[str, Any]:
        return {"name": self.name}


def test_pydantic_compatibility_fakes_v1_model_and_type_adapter_fallback(monkeypatch) -> None:
    assert pydantic_compat.pydantic_model_fields(LegacyPydanticModel) == ("name",)
    model = pydantic_compat.pydantic_validate_model(LegacyPydanticModel, {"name": "Ada"})
    assert pydantic_compat.pydantic_dump_model(model) == {"name": "Ada"}

    monkeypatch.setattr(pydantic_compat, "TypeAdapter", None)
    monkeypatch.setattr(pydantic_compat, "parse_obj_as", lambda annotation, value: annotation(value))

    assert pydantic_compat.pydantic_validate_type(int, "42") == 42
