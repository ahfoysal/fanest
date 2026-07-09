from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiscoveredProvider:
    token: Any
    instance: Any
    module_key: Any | None = None
    module_type: type | None = None
    provider: Any | None = None
    metatype: type | None = None
    metadata: dict[str, Any] | None = None


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
            instance = self.container.resolve(token)
            discovered.append(
                DiscoveredProvider(
                    token=token,
                    instance=instance,
                    provider=provider,
                    metatype=self._metatype(provider, instance),
                    metadata=self._metadata(provider, instance),
                )
            )
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
                        provider=provider,
                        metatype=self._metatype(provider),
                        metadata=self._metadata(provider),
                    )
                )
        return discovered

    def get_controllers(self) -> list[type]:
        return list(self.controllers)

    def with_metadata(self, key: str) -> list[DiscoveredProvider]:
        return [
            provider
            for provider in self.get_providers()
            if hasattr(provider.metatype or provider.instance.__class__, key)
            or key in (provider.metadata or {})
        ]

    def _metatype(self, provider: Any, instance: Any | None = None) -> type | None:
        use_class = getattr(provider, "use_class", None)
        if use_class is not None:
            return use_class
        if isinstance(provider, type):
            return provider
        if instance is not None:
            return instance.__class__
        return None

    def _metadata(self, provider: Any, instance: Any | None = None) -> dict[str, Any]:
        metatype = self._metatype(provider, instance)
        metadata: dict[str, Any] = {}
        if metatype is not None:
            metadata.update(getattr(metatype, "__fanest_metadata__", {}))
        if instance is not None:
            metadata.update(getattr(instance.__class__, "__fanest_metadata__", {}))
        return metadata
