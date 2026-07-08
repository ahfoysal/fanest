from typing import Any


class Reflector:
    def get(self, key: str, target: Any, default: Any = None) -> Any:
        metadata = self._metadata(target)
        return metadata.get(key, default)

    def get_all(self, key: str, targets: list[Any]) -> list[Any]:
        return [value for target in targets if (value := self.get(key, target)) is not None]

    def get_all_and_override(self, key: str, targets: list[Any], default: Any = None) -> Any:
        for target in targets:
            value = self.get(key, target)
            if value is not None:
                return value
        return default

    def get_all_and_merge(self, key: str, targets: list[Any]) -> list[Any]:
        merged: list[Any] = []
        for value in self.get_all(key, targets):
            if isinstance(value, list):
                merged.extend(value)
            else:
                merged.append(value)
        return merged

    def _metadata(self, target: Any) -> dict[str, Any]:
        if hasattr(target, "__fanest_metadata__"):
            return getattr(target, "__fanest_metadata__")
        func = getattr(target, "__func__", None)
        if func is not None and hasattr(func, "__fanest_metadata__"):
            return getattr(func, "__fanest_metadata__")
        return {}
