from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiscoveredProvider:
    token: Any
    instance: Any
    module_key: Any | None = None
    module_type: type | None = None


class DiscoveryService:
    def __init__(
        self,
        container: Any,
        providers: list[Any],
        controllers: list[type],
        records: dict[Any, Any] | None = None,
    ) -> None:
        self.container = container
        self.providers = providers
        self.controllers = controllers
        self.records = records or {}

    def get_providers(self) -> list[DiscoveredProvider]:
        if self.records:
            return self._get_module_providers()
        discovered: list[DiscoveredProvider] = []
        for provider in self.providers:
            token = self.container.provider_token(provider)
            discovered.append(DiscoveredProvider(token=token, instance=self.container.resolve(token)))
        return discovered

    def _get_module_providers(self) -> list[DiscoveredProvider]:
        discovered: list[DiscoveredProvider] = []
        seen: set[tuple[Any, Any]] = set()
        for module_key, record in self.records.items():
            for provider in [*record.metadata.providers, *record.metadata.gateways]:
                token = self.container.provider_token(provider)
                key = (module_key, token)
                if key in seen:
                    continue
                seen.add(key)
                discovered.append(
                    DiscoveredProvider(
                        token=token,
                        instance=self.container.resolve(token, module_key=module_key),
                        module_key=module_key,
                        module_type=record.module_type,
                    )
                )
        return discovered

    def get_controllers(self) -> list[type]:
        return list(self.controllers)

    def with_metadata(self, key: str) -> list[DiscoveredProvider]:
        return [
            provider
            for provider in self.get_providers()
            if hasattr(provider.instance.__class__, key)
        ]
