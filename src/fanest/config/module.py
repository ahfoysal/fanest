import os
import re
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar, cast as typing_cast

from fanest import Inject, Injectable, Module, use_value
from fanest.common.pydantic_compat import (
    BaseModel,
    pydantic_dump_model,
    pydantic_validate_model,
    pydantic_validate_type,
)
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

CONFIG_VALUES = token("CONFIG_VALUES")
T = TypeVar("T")
_ENV_EXPANSION_PATTERN = re.compile(r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))")


@Injectable()
class ConfigService:
    def __init__(self, values: dict[str, Any] = Inject(CONFIG_VALUES)):
        self._values = values

    @staticmethod
    def read_env_file(env_file: str, *, encoding: str = "utf-8", expand_variables: bool = False) -> dict[str, str]:
        path = Path(env_file)
        if not path.exists():
            return {}
        values: dict[str, str] = {}
        for line in path.read_text(encoding=encoding).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.startswith("export "):
                stripped = stripped.removeprefix("export ").strip()
            key, value = stripped.split("=", 1)
            parsed_value = _parse_env_value(value)
            if expand_variables:
                parsed_value = _expand_env_value(parsed_value, values)
            values[key.strip()] = parsed_value
        return values

    def get(self, key: str, default: T | None = None, *, cast: type[T] | None = None) -> Any:
        value = self._lookup(key, default)
        if cast is not None and value is not None:
            if cast is bool and isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                if normalized in {"0", "false", "no", "off"}:
                    return False
                raise ValueError(f"Cannot cast config value {key!r} to bool")
            return typing_cast(Callable[[Any], T], cast)(value)
        return value

    def has(self, key: str) -> bool:
        return self._lookup(key, default=...) is not ...

    def get_many(self, *keys: str) -> dict[str, Any]:
        return {key: self.get(key) for key in keys}

    def _lookup(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        current: Any = self._values
        for part in key.split("."):
            if isinstance(current, list):
                if not part.isdigit():
                    return default
                index = int(part)
                if index >= len(current):
                    return default
                current = current[index]
                continue
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

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
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return typing_cast(T, pydantic_validate_model(schema, self._values))
        return typing_cast(T, pydantic_validate_type(schema, self._values))


class ConfigModule:
    @staticmethod
    def _load_values(
        *,
        env_file: str | list[str] | None,
        values: dict[str, Any] | None = None,
        env_file_encoding: str = "utf-8",
        expand_variables: bool = False,
    ) -> dict[str, Any]:
        config_values: dict[str, Any] = {}
        env_files = [env_file] if isinstance(env_file, str) else env_file or []
        for file in env_files:
            config_values.update(
                ConfigService.read_env_file(
                    file,
                    encoding=env_file_encoding,
                    expand_variables=expand_variables,
                )
            )
        config_values.update(os.environ)
        config_values.update(values or {})
        return config_values

    @staticmethod
    def for_root(
        *,
        env_file: str | list[str] | None = ".env",
        env_file_encoding: str = "utf-8",
        expand_variables: bool = False,
        schema: type[BaseModel] | None = None,
        values: dict[str, Any] | None = None,
        is_global: bool = False,
    ) -> type:
        config_values = ConfigModule._load_values(
            env_file=env_file,
            env_file_encoding=env_file_encoding,
            expand_variables=expand_variables,
            values=values,
        )
        if schema is not None:
            config_values = pydantic_dump_model(pydantic_validate_model(schema, config_values))

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
        use_factory: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]],
        inject: list[Any] | None = None,
        env_file: str | list[str] | None = ".env",
        env_file_encoding: str = "utf-8",
        expand_variables: bool = False,
        schema: type[BaseModel] | None = None,
        is_global: bool = False,
    ) -> type:
        async def load_values(*dependencies: Any) -> dict[str, Any]:
            config_values = ConfigModule._load_values(
                env_file=env_file,
                env_file_encoding=env_file_encoding,
                expand_variables=expand_variables,
            )
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await result
            config_values.update(result or {})
            if schema is not None:
                return pydantic_dump_model(pydantic_validate_model(schema, config_values))
            return config_values

        @Module(
            providers=[provider_factory(CONFIG_VALUES, load_values, inject=inject or []), ConfigService],
            exports=[ConfigService],
            global_module=is_global,
        )
        class DynamicConfigModule:
            pass

        return DynamicConfigModule


def _parse_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'} and value[-1:] == value[0]:
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def _expand_env_value(value: str, file_values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group("braced") or match.group("plain") or ""
        return file_values.get(key, os.environ.get(key, match.group(0)))

    return _ENV_EXPANSION_PATTERN.sub(replace, value)
