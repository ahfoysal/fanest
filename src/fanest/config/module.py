import os
from pathlib import Path

from fanest import Injectable, Module


@Injectable()
class ConfigService:
    _values: dict[str, str] = {}

    @classmethod
    def configure(cls, *, env_file: str | None = None) -> None:
        values = dict(os.environ)
        if env_file:
            values.update(cls._read_env_file(env_file))
        cls._values = values

    @classmethod
    def _read_env_file(cls, env_file: str) -> dict[str, str]:
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

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)

    def get_required(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(f"Missing required config value: {key}")
        return value


class ConfigModule:
    @staticmethod
    def for_root(*, env_file: str | None = ".env") -> type:
        ConfigService.configure(env_file=env_file)

        @Module(providers=[ConfigService], exports=[ConfigService])
        class DynamicConfigModule:
            pass

        return DynamicConfigModule
