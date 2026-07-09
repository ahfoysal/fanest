import pytest
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, UseGuards
from fanest.auth import AuthModule, JwtAuthGuard
from fanest.config import ConfigModule, ConfigService


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


def test_auth_module_rejects_empty_and_unsigned_jwt_configuration():
    with pytest.raises(ValueError, match="non-empty JWT secret"):
        AuthModule.for_root(secret="")

    with pytest.raises(ValueError, match="unsigned JWT algorithms"):
        AuthModule.for_root(secret="valid-secret-value-with-enough-entropy", algorithm="none")


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
