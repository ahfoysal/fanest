import inspect
from contextvars import ContextVar
from typing import Any, get_type_hints

from fanest.core.metadata import (
    ClassProvider,
    ExistingProvider,
    FactoryProvider,
    ForwardRef,
    InjectMarker,
    ProviderDefinition,
    ValueProvider,
)
from fanest.core.module_ref import ModuleRef
from fanest.core.reflector import Reflector

_request_instances: ContextVar[dict[Any, Any] | None] = ContextVar(
    "fanest_request_instances", default=None
)


class FaNestContainer:
    def __init__(self) -> None:
        self._providers: dict[Any, ProviderDefinition] = {}
        self._instances: dict[Any, Any] = {}
        self._resolving: set[Any] = set()
        self.register(ValueProvider(provide=ModuleRef, use_value=ModuleRef(self)))
        self.register(ValueProvider(provide=Reflector, use_value=Reflector()))

    def register(self, provider: ProviderDefinition) -> None:
        token = self.provider_token(provider)
        self._providers[token] = provider

    def begin_request(self):
        return _request_instances.set({})

    def end_request(self, token: Any) -> None:
        _request_instances.reset(token)

    def override(self, token: Any, value: Any) -> None:
        if isinstance(value, (ClassProvider, ValueProvider, FactoryProvider, ExistingProvider)):
            self._providers[token] = value
            self._instances.pop(token, None)
            return
        if inspect.isclass(value):
            self._providers[token] = ClassProvider(provide=token, use_class=value)
            self._instances.pop(token, None)
            return
        self._instances[token] = value

    def resolve(self, token: Any) -> Any:
        provider = self._providers.get(token)
        if provider is None:
            if not inspect.isclass(token):
                raise KeyError(f"No provider registered for token {token!r}")
            provider = token

        scope = self._provider_scope(provider)
        request_cache = _request_instances.get()

        if scope == "request" and request_cache is not None and token in request_cache:
            return request_cache[token]
        if scope == "singleton" and token in self._instances:
            return self._instances[token]

        if token in self._resolving:
            raise RuntimeError(f"Circular dependency detected while resolving {token!r}")
        self._resolving.add(token)
        try:
            instance = self._resolve_provider(provider)
        finally:
            self._resolving.remove(token)
        if scope == "request" and request_cache is not None:
            request_cache[token] = instance
        elif scope == "singleton":
            self._instances[token] = instance
        return instance

    async def resolve_async(self, token: Any) -> Any:
        provider = self._providers.get(token)
        if provider is None:
            if not inspect.isclass(token):
                raise KeyError(f"No provider registered for token {token!r}")
            provider = token

        scope = self._provider_scope(provider)
        request_cache = _request_instances.get()

        if scope == "request" and request_cache is not None and token in request_cache:
            return request_cache[token]
        if scope == "singleton" and token in self._instances:
            return self._instances[token]

        if token in self._resolving:
            raise RuntimeError(f"Circular dependency detected while resolving {token!r}")
        self._resolving.add(token)
        try:
            instance = await self._resolve_provider_async(provider)
        finally:
            self._resolving.remove(token)
        if scope == "request" and request_cache is not None:
            request_cache[token] = instance
        elif scope == "singleton":
            self._instances[token] = instance
        return instance

    def provider_token(self, provider: ProviderDefinition) -> Any:
        if isinstance(provider, ForwardRef):
            return self.provider_token(provider.factory())
        if isinstance(provider, (ClassProvider, ValueProvider, FactoryProvider, ExistingProvider)):
            return provider.provide
        return provider

    def _resolve_provider(self, provider: ProviderDefinition) -> Any:
        if isinstance(provider, ForwardRef):
            return self._resolve_provider(provider.factory())
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

    async def _resolve_provider_async(self, provider: ProviderDefinition) -> Any:
        if isinstance(provider, ForwardRef):
            return await self._resolve_provider_async(provider.factory())
        if isinstance(provider, ClassProvider):
            return await self._instantiate_async(provider.use_class)
        if isinstance(provider, ValueProvider):
            return provider.use_value
        if isinstance(provider, ExistingProvider):
            return await self.resolve_async(provider.use_existing)
        if isinstance(provider, FactoryProvider):
            dependencies = [await self._resolve_injected_token_async(token) for token in provider.inject]
            result = provider.use_factory(*dependencies)
            if inspect.isawaitable(result):
                return await result
            return result
        if inspect.isclass(provider):
            return await self._instantiate_async(provider)
        return provider

    def _provider_scope(self, provider: ProviderDefinition) -> str:
        if isinstance(provider, ClassProvider):
            return self._class_scope(provider.use_class)
        if inspect.isclass(provider):
            return self._class_scope(provider)
        return "singleton"

    def _class_scope(self, provider: type) -> str:
        metadata = getattr(provider, "__fanest_provider__", None)
        return getattr(metadata, "scope", "singleton")

    def _resolve_injected_token(self, marker: Any) -> Any:
        if isinstance(marker, InjectMarker):
            try:
                return self.resolve(self._unwrap_token(marker.token))
            except Exception:
                if marker.optional:
                    return marker.default
                raise
        return self.resolve(self._unwrap_token(marker))

    async def _resolve_injected_token_async(self, marker: Any) -> Any:
        if isinstance(marker, InjectMarker):
            try:
                return await self.resolve_async(self._unwrap_token(marker.token))
            except Exception:
                if marker.optional:
                    return marker.default
                raise
        return await self.resolve_async(self._unwrap_token(marker))

    def _unwrap_token(self, token: Any) -> Any:
        if isinstance(token, ForwardRef):
            return token.factory()
        return token

    def instantiate(self, provider: type) -> Any:
        return self._instantiate(provider)

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
            if parameter.default is not inspect.Parameter.empty:
                kwargs[name] = parameter.default
                continue
            annotation = type_hints.get(name, parameter.annotation)
            if annotation is inspect.Parameter.empty:
                raise TypeError(
                    f"Cannot resolve dependency '{name}' for {provider.__name__}. "
                    "Add a type annotation or register an explicit provider."
                )
            kwargs[name] = self.resolve(annotation)

        return provider(**kwargs)

    async def _instantiate_async(self, provider: type) -> Any:
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
                kwargs[name] = await self._resolve_injected_token_async(parameter.default)
                continue
            if parameter.default is not inspect.Parameter.empty:
                kwargs[name] = parameter.default
                continue
            annotation = type_hints.get(name, parameter.annotation)
            if annotation is inspect.Parameter.empty:
                raise TypeError(
                    f"Cannot resolve dependency '{name}' for {provider.__name__}. "
                    "Add a type annotation or register an explicit provider."
                )
            kwargs[name] = await self.resolve_async(annotation)

        return provider(**kwargs)
