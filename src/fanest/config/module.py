import inspect
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, TypeAdapter

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

CONFIG_VALUES = token("CONFIG_VALUES")
T = TypeVar("T")


@Injectable()
class ConfigService:
    def __init__(self, values: dict[str, Any] = Inject(CONFIG_VALUES)):
        self._values = values

    @staticmethod
    def read_env_file(env_file: str) -> dict[str, str]:
        path = Path(env_file)
        if not path.exists():
            return {}
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def get(self, key: str, default: T | None = None, *, cast: type[T] | None = None) -> Any:
        value = self._values.get(key, default)
        if cast is not None and value is not None:
            if cast is bool and isinstance(value, str):
                return value.lower() in {"1", "true", "yes", "on"}
            return cast(value)
        return value

    def get_required(self, key: str, *, cast: type[T] | None = None) -> Any:
        value = self.get(key)
        if value is None:
            raise KeyError(f"Missing required config value: {key}")
        if cast is not None:
            return self.get(key, cast=cast)
        return value

    def get_or_throw(self, key: str, *, cast: type[T] | None = None) -> Any:
        return self.get_required(key, cast=cast)

    def validate(self, schema: type[T]) -> T:
        return TypeAdapter(schema).validate_python(self._values)


class ConfigModule:
    @staticmethod
    def for_root(
        *,
        env_file: str | list[str] | None = ".env",
        schema: type[BaseModel] | None = None,
        values: dict[str, Any] | None = None,
        is_global: bool = False,
    ) -> type:
        config_values: dict[str, Any] = dict(os.environ)
        env_files = [env_file] if isinstance(env_file, str) else env_file or []
        for file in env_files:
            config_values.update(ConfigService.read_env_file(file))
        config_values.update(values or {})
        if schema is not None:
            config_values = schema.model_validate(config_values).model_dump()

        @Module(
            providers=[use_value(CONFIG_VALUES, config_values), ConfigService],
            exports=[ConfigService],
            global_module=is_global,
        )
        class DynamicConfigModule:
            pass

        return DynamicConfigModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any]],
        inject: list[Any] | None = None,
        env_file: str | list[str] | None = ".env",
        schema: type[BaseModel] | None = None,
        is_global: bool = False,
    ) -> type:
        async def load_values(*dependencies: Any) -> dict[str, Any]:
            config_values: dict[str, Any] = dict(os.environ)
            env_files = [env_file] if isinstance(env_file, str) else env_file or []
            for file in env_files:
                config_values.update(ConfigService.read_env_file(file))
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await result
            config_values.update(result or {})
            if schema is not None:
                return schema.model_validate(config_values).model_dump()
            return config_values

        @Module(
            providers=[provider_factory(CONFIG_VALUES, load_values, inject=inject or []), ConfigService],
            exports=[ConfigService],
            global_module=is_global,
        )
        class DynamicConfigModule:
            pass

        return DynamicConfigModule
