import inspect
from typing import Any, get_type_hints

from fanest.core.metadata import (
    ClassProvider,
    ExistingProvider,
    FactoryProvider,
    InjectMarker,
    ProviderDefinition,
    ValueProvider,
)


class FaNestContainer:
    def __init__(self) -> None:
        self._providers: dict[Any, ProviderDefinition] = {}
        self._instances: dict[Any, Any] = {}
        self._resolving: set[Any] = set()

    def register(self, provider: ProviderDefinition) -> None:
        token = self.provider_token(provider)
        self._providers[token] = provider

    def override(self, token: Any, value: Any) -> None:
        if inspect.isclass(value):
            self._providers[token] = ClassProvider(provide=token, use_class=value)
            self._instances.pop(token, None)
            return
        self._instances[token] = value

    def resolve(self, token: Any) -> Any:
        if token in self._instances:
            return self._instances[token]

        provider = self._providers.get(token)
        if provider is None:
            if not inspect.isclass(token):
                raise KeyError(f"No provider registered for token {token!r}")
            provider = token
        if token in self._resolving:
            raise RuntimeError(f"Circular dependency detected while resolving {token!r}")
        self._resolving.add(token)
        try:
            instance = self._resolve_provider(provider)
        finally:
            self._resolving.remove(token)
        self._instances[token] = instance
        return instance

    def provider_token(self, provider: ProviderDefinition) -> Any:
        if isinstance(provider, (ClassProvider, ValueProvider, FactoryProvider, ExistingProvider)):
            return provider.provide
        return provider

    def _resolve_provider(self, provider: ProviderDefinition) -> Any:
        if isinstance(provider, ClassProvider):
            return self._instantiate(provider.use_class)
        if isinstance(provider, ValueProvider):
            return provider.use_value
        if isinstance(provider, ExistingProvider):
            return self.resolve(provider.use_existing)
        if isinstance(provider, FactoryProvider):
            dependencies = [self._resolve_injected_token(token) for token in provider.inject]
            result = provider.use_factory(*dependencies)
            return result
        if inspect.isclass(provider):
            return self._instantiate(provider)
        return provider

    def _resolve_injected_token(self, marker: Any) -> Any:
        if isinstance(marker, InjectMarker):
            try:
                return self.resolve(marker.token)
            except Exception:
                if marker.optional:
                    return marker.default
                raise
        return self.resolve(marker)

    def _instantiate(self, provider: type) -> Any:
        signature = inspect.signature(provider.__init__)
        type_hints = get_type_hints(provider.__init__)
        kwargs: dict[str, Any] = {}

        for name, parameter in signature.parameters.items():
            if name == "self":
                continue
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if isinstance(parameter.default, InjectMarker):
                kwargs[name] = self._resolve_injected_token(parameter.default)
                continue
            annotation = type_hints.get(name, parameter.annotation)
            if annotation is inspect.Parameter.empty:
                raise TypeError(
                    f"Cannot resolve dependency '{name}' for {provider.__name__}. "
                    "Add a type annotation or register an explicit provider."
                )
            kwargs[name] = self.resolve(annotation)

        return provider(**kwargs)
