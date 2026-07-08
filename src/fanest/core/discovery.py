from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiscoveredProvider:
    token: Any
    instance: Any


class DiscoveryService:
    def __init__(self, container: Any, providers: list[Any], controllers: list[type]) -> None:
        self.container = container
        self.providers = providers
        self.controllers = controllers

    def get_providers(self) -> list[DiscoveredProvider]:
        discovered: list[DiscoveredProvider] = []
        for provider in self.providers:
            token = self.container.provider_token(provider)
            discovered.append(DiscoveredProvider(token=token, instance=self.container.resolve(token)))
        return discovered

    def get_controllers(self) -> list[type]:
        return list(self.controllers)

    def with_metadata(self, key: str) -> list[DiscoveredProvider]:
        return [
            provider
            for provider in self.get_providers()
            if hasattr(provider.instance.__class__, key)
        ]
