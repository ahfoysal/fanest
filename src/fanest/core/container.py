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
from fanest.core.enhancers import APP_ENHANCER_TOKENS
from fanest.core.module_ref import ModuleRef
from fanest.core.reflector import Reflector
from fanest.schedule.registry import SchedulerRegistry
from fanest.websockets import SocketIoServer, WebSocketManager

_request_instances: ContextVar[dict[Any, Any] | None] = ContextVar(
    "fanest_request_instances", default=None
)


class ForwardRefProxy:
    def __init__(self, container: "FaNestContainer", token: Any, module_key: Any | None = None) -> None:
        object.__setattr__(self, "_fanest_container", container)
        object.__setattr__(self, "_fanest_token", token)
        object.__setattr__(self, "_fanest_module_key", module_key)

    def _target(self) -> Any:
        return object.__getattribute__(self, "_fanest_container").resolve(
            object.__getattribute__(self, "_fanest_token"),
            module_key=object.__getattribute__(self, "_fanest_module_key"),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._target(), name, value)

    def __repr__(self) -> str:
        return f"<ForwardRefProxy token={object.__getattribute__(self, '_fanest_token')!r}>"


class FaNestContainer:
    def __init__(self) -> None:
        self._providers: dict[Any, ProviderDefinition] = {}
        self._module_providers: dict[Any, dict[Any, ProviderDefinition]] = {}
        self._module_imports: dict[Any, list[Any]] = {}
        self._module_exports: dict[Any, set[Any]] = {}
        self._global_modules: set[Any] = set()
        self._multi_providers: dict[Any, list[ProviderDefinition]] = {}
        self._instances: dict[Any, Any] = {}
        self._dependency_cache: dict[type, tuple[dict[str, inspect.Parameter], dict[str, Any]]] = {}
        self._provider_dependency_cache: dict[Any, list[Any]] = {}
        self._scope_cache: dict[Any, str] = {}
        self._resolving: set[Any] = set()
        self.register(ValueProvider(provide=ModuleRef, use_value=ModuleRef(self)))
        self.register(ValueProvider(provide=Reflector, use_value=Reflector()))
        self.register(ValueProvider(provide=SchedulerRegistry, use_value=SchedulerRegistry()))
        websocket_manager = WebSocketManager()
        self.register(ValueProvider(provide=WebSocketManager, use_value=websocket_manager))
        self.register(ValueProvider(provide=SocketIoServer, use_value=SocketIoServer(websocket_manager)))

    def register(self, provider: ProviderDefinition) -> None:
        token = self.provider_token(provider)
        self._invalidate_provider_cache(token)
        if token in APP_ENHANCER_TOKENS:
            self._multi_providers.setdefault(token, []).append(provider)
            return
        self._providers[token] = provider

    def register_module(
        self,
        module_key: Any,
        *,
        providers: list[ProviderDefinition],
        imports: list[Any],
        exports: set[Any],
        global_module: bool = False,
    ) -> None:
        module_providers = self._module_providers.setdefault(module_key, {})
        for provider in providers:
            token = self.provider_token(provider)
            module_providers[token] = provider
        self._module_imports[module_key] = list(imports)
        self._module_exports[module_key] = set(exports)
        if global_module:
            self._global_modules.add(module_key)

    def begin_request(self):
        return _request_instances.set({})

    def end_request(self, token: Any) -> None:
        _request_instances.reset(token)

    def override(self, token: Any, value: Any) -> None:
        provider = self._override_provider(token, value)
        if token in APP_ENHANCER_TOKENS:
            self._multi_providers[token] = [provider]
            self._scope_cache.clear()
            return
        replaced_module_provider = False
        for module_key, providers in self._module_providers.items():
            if token not in providers:
                continue
            providers[token] = provider
            self._instances.pop(self._cache_key(module_key, token), None)
            replaced_module_provider = True
        self._providers[token] = provider
        self._invalidate_provider_cache(token)
        self._instances.pop(token, None)
        if not replaced_module_provider and isinstance(provider, ValueProvider):
            self._instances[token] = provider.use_value

    def _override_provider(self, token: Any, value: Any) -> ProviderDefinition:
        if isinstance(value, (ClassProvider, ValueProvider, FactoryProvider, ExistingProvider)):
            return value
        if inspect.isclass(value):
            return ClassProvider(provide=token, use_class=value)
        return ValueProvider(provide=token, use_value=value)

    def resolve_all(self, token: Any) -> list[Any]:
        return [self._resolve_provider(provider) for provider in self._multi_providers.get(token, [])]

    def has_provider(self, token: Any) -> bool:
        token = self._unwrap_token(token)
        return token in self._providers or token in self._multi_providers

    def provider_tokens(self) -> tuple[Any, ...]:
        return tuple([*self._providers.keys(), *self._multi_providers.keys()])

    def describe_provider(self, token: Any) -> dict[str, Any]:
        token = self._unwrap_token(token)
        provider = self._providers.get(token)
        multi = self._multi_providers.get(token)
        if provider is None and multi is None:
            raise KeyError(token)
        if multi is not None:
            return {
                "token": token,
                "scope": "singleton",
                "type": "multi",
                "multi": True,
                "count": len(multi),
            }
        assert provider is not None
        return {
            "token": token,
            "scope": self._effective_scope(token, provider),
            "type": self._provider_kind(provider),
            "multi": False,
            "dependencies": tuple(self._unwrap_token(dependency) for dependency in self._provider_dependencies(provider)),
            "resolved": token in self._instances,
        }

    def resolve(self, token: Any, module_key: Any | None = None) -> Any:
        token = self._unwrap_token(token)
        owner_key, provider = self._locate_provider(token, module_key)
        if provider is None:
            if not inspect.isclass(token):
                raise KeyError(token)
            provider = token
            owner_key = module_key

        scope = self._effective_scope(token, provider, module_key=owner_key)
        request_cache = _request_instances.get()
        cache_key = self._cache_key(owner_key, token)

        if scope == "request" and request_cache is not None and cache_key in request_cache:
            return request_cache[cache_key]
        if scope == "singleton" and cache_key in self._instances:
            return self._instances[cache_key]

        if cache_key in self._resolving:
            raise RuntimeError(f"Circular dependency detected while resolving {token!r}")
        self._resolving.add(cache_key)
        try:
            instance = self._resolve_provider(provider, module_key=owner_key)
        finally:
            self._resolving.remove(cache_key)
        if scope == "request" and request_cache is not None:
            request_cache[cache_key] = instance
        elif scope == "singleton":
            self._instances[cache_key] = instance
        return instance

    async def resolve_async(self, token: Any, module_key: Any | None = None) -> Any:
        token = self._unwrap_token(token)
        owner_key, provider = self._locate_provider(token, module_key)
        if provider is None:
            if not inspect.isclass(token):
                raise KeyError(token)
            provider = token
            owner_key = module_key

        scope = self._effective_scope(token, provider, module_key=owner_key)
        request_cache = _request_instances.get()
        cache_key = self._cache_key(owner_key, token)

        if scope == "request" and request_cache is not None and cache_key in request_cache:
            return request_cache[cache_key]
        if scope == "singleton" and cache_key in self._instances:
            return self._instances[cache_key]

        if cache_key in self._resolving:
            raise RuntimeError(f"Circular dependency detected while resolving {token!r}")
        self._resolving.add(cache_key)
        try:
            instance = await self._resolve_provider_async(provider, module_key=owner_key)
        finally:
            self._resolving.remove(cache_key)
        if scope == "request" and request_cache is not None:
            request_cache[cache_key] = instance
        elif scope == "singleton":
            self._instances[cache_key] = instance
        return instance

    def provider_token(self, provider: ProviderDefinition) -> Any:
        if isinstance(provider, ForwardRef):
            return self.provider_token(provider.factory())
        if isinstance(provider, (ClassProvider, ValueProvider, FactoryProvider, ExistingProvider)):
            return provider.provide
        return provider

    def _resolve_provider(self, provider: ProviderDefinition, module_key: Any | None = None) -> Any:
        if isinstance(provider, ForwardRef):
            return self._resolve_provider(provider.factory(), module_key=module_key)
        if isinstance(provider, ClassProvider):
            return self._instantiate(provider.use_class, module_key=module_key)
        if isinstance(provider, ValueProvider):
            return provider.use_value
        if isinstance(provider, ExistingProvider):
            return self.resolve(provider.use_existing, module_key=module_key)
        if isinstance(provider, FactoryProvider):
            dependencies = [self._resolve_injected_token(token, module_key=module_key) for token in provider.inject]
            result = provider.use_factory(*dependencies)
            return result
        if inspect.isclass(provider):
            return self._instantiate(provider, module_key=module_key)
        return provider

    async def _resolve_provider_async(self, provider: ProviderDefinition, module_key: Any | None = None) -> Any:
        if isinstance(provider, ForwardRef):
            return await self._resolve_provider_async(provider.factory(), module_key=module_key)
        if isinstance(provider, ClassProvider):
            return await self._instantiate_async(provider.use_class, module_key=module_key)
        if isinstance(provider, ValueProvider):
            return provider.use_value
        if isinstance(provider, ExistingProvider):
            return await self.resolve_async(provider.use_existing, module_key=module_key)
        if isinstance(provider, FactoryProvider):
            dependencies = [
                await self._resolve_injected_token_async(token, module_key=module_key)
                for token in provider.inject
            ]
            result = provider.use_factory(*dependencies)
            if inspect.isawaitable(result):
                return await result
            return result
        if inspect.isclass(provider):
            return await self._instantiate_async(provider, module_key=module_key)
        return provider

    def _provider_scope(self, provider: ProviderDefinition) -> str:
        if isinstance(provider, ClassProvider):
            return self._class_scope(provider.use_class)
        if inspect.isclass(provider):
            return self._class_scope(provider)
        return "singleton"

    def _provider_kind(self, provider: ProviderDefinition) -> str:
        if isinstance(provider, ForwardRef):
            return self._provider_kind(provider.factory())
        if isinstance(provider, ClassProvider):
            return "class"
        if isinstance(provider, ValueProvider):
            return "value"
        if isinstance(provider, ExistingProvider):
            return "existing"
        if isinstance(provider, FactoryProvider):
            return "factory"
        if inspect.isclass(provider):
            return "class"
        return "value"

    def _effective_scope(
        self,
        token: Any,
        provider: ProviderDefinition,
        seen: set[Any] | None = None,
        module_key: Any | None = None,
    ) -> str:
        scope_key = self._cache_key(module_key, token)
        if seen is None and scope_key in self._scope_cache:
            return self._scope_cache[scope_key]
        own_scope = self._provider_scope(provider)
        if own_scope != "singleton":
            if seen is None:
                self._scope_cache[scope_key] = own_scope
            return own_scope
        seen = seen or set()
        if scope_key in seen:
            return own_scope
        seen.add(scope_key)
        for dependency in self._provider_dependencies(provider):
            dependency = self._unwrap_token(dependency)
            dependency_module_key, dependency_provider = self._locate_provider(dependency, module_key)
            if dependency_provider is None:
                if not inspect.isclass(dependency):
                    continue
                dependency_provider = dependency
            if self._effective_scope(
                dependency,
                dependency_provider,
                seen,
                module_key=dependency_module_key,
            ) == "request":
                if len(seen) == 1:
                    self._scope_cache[scope_key] = "request"
                return "request"
        if len(seen) == 1:
            self._scope_cache[scope_key] = own_scope
        return own_scope

    def _provider_dependencies(self, provider: ProviderDefinition) -> list[Any]:
        provider_key = self.provider_token(provider) if not isinstance(provider, ForwardRef) else provider.factory()
        if provider_key in self._provider_dependency_cache:
            return self._provider_dependency_cache[provider_key]
        if isinstance(provider, ExistingProvider):
            dependencies = [provider.use_existing]
        elif isinstance(provider, FactoryProvider):
            dependencies = list(provider.inject)
        elif isinstance(provider, ClassProvider):
            dependencies = self._class_dependencies(provider.use_class)
        elif inspect.isclass(provider):
            dependencies = self._class_dependencies(provider)
        else:
            dependencies = []
        self._provider_dependency_cache[provider_key] = dependencies
        return dependencies

    def _class_dependencies(self, provider: type) -> list[Any]:
        parameters, type_hints = self._constructor_metadata(provider)
        dependencies: list[Any] = []
        for name, parameter in parameters.items():
            if name == "self" or parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if isinstance(parameter.default, InjectMarker):
                dependencies.append(parameter.default.token)
                continue
            if parameter.default is not inspect.Parameter.empty:
                continue
            annotation = type_hints.get(name, parameter.annotation)
            if annotation is not inspect.Parameter.empty:
                dependencies.append(annotation)
        return dependencies

    def _class_scope(self, provider: type) -> str:
        metadata = getattr(provider, "__fanest_provider__", None)
        return getattr(metadata, "scope", "singleton")

    def _resolve_injected_token(self, marker: Any, module_key: Any | None = None) -> Any:
        if isinstance(marker, InjectMarker):
            try:
                token = self._unwrap_token(marker.token)
                owner_key, _ = self._locate_provider(token, module_key)
                if self._cache_key(owner_key, token) in self._resolving:
                    return ForwardRefProxy(self, token, module_key=owner_key)
                return self.resolve(token, module_key=module_key)
            except Exception:
                if marker.optional:
                    return marker.default
                raise
        return self.resolve(self._unwrap_token(marker), module_key=module_key)

    async def _resolve_injected_token_async(self, marker: Any, module_key: Any | None = None) -> Any:
        if isinstance(marker, InjectMarker):
            try:
                token = self._unwrap_token(marker.token)
                owner_key, _ = self._locate_provider(token, module_key)
                if self._cache_key(owner_key, token) in self._resolving:
                    return ForwardRefProxy(self, token, module_key=owner_key)
                return await self.resolve_async(token, module_key=module_key)
            except Exception:
                if marker.optional:
                    return marker.default
                raise
        return await self.resolve_async(self._unwrap_token(marker), module_key=module_key)

    def _unwrap_token(self, token: Any) -> Any:
        if isinstance(token, ForwardRef):
            return token.factory()
        return token

    def instantiate(self, provider: type) -> Any:
        return self._instantiate(provider)

    async def instantiate_async(self, provider: type) -> Any:
        return await self._instantiate_async(provider)

    def _instantiate(self, provider: type, module_key: Any | None = None) -> Any:
        parameters, type_hints = self._constructor_metadata(provider)
        kwargs: dict[str, Any] = {}

        for name, parameter in parameters.items():
            if name == "self":
                continue
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if isinstance(parameter.default, InjectMarker):
                kwargs[name] = self._resolve_injected_token(parameter.default, module_key=module_key)
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
            kwargs[name] = self.resolve(annotation, module_key=module_key)

        return provider(**kwargs)

    async def _instantiate_async(self, provider: type, module_key: Any | None = None) -> Any:
        parameters, type_hints = self._constructor_metadata(provider)
        kwargs: dict[str, Any] = {}

        for name, parameter in parameters.items():
            if name == "self":
                continue
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            if isinstance(parameter.default, InjectMarker):
                kwargs[name] = await self._resolve_injected_token_async(
                    parameter.default,
                    module_key=module_key,
                )
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
            kwargs[name] = await self.resolve_async(annotation, module_key=module_key)

        return provider(**kwargs)

    def _constructor_metadata(self, provider: type) -> tuple[dict[str, inspect.Parameter], dict[str, Any]]:
        cached = self._dependency_cache.get(provider)
        if cached is not None:
            return cached
        signature = inspect.signature(provider.__init__)
        type_hints = get_type_hints(provider.__init__)
        parameters = dict(signature.parameters)
        metadata = (parameters, type_hints)
        self._dependency_cache[provider] = metadata
        return metadata

    def _invalidate_provider_cache(self, token: Any) -> None:
        token = self._unwrap_token(token)
        self._provider_dependency_cache.pop(token, None)
        self._scope_cache.clear()

    def _locate_provider(
        self,
        token: Any,
        module_key: Any | None = None,
        seen: set[Any] | None = None,
    ) -> tuple[Any | None, ProviderDefinition | None]:
        if module_key is None:
            module_matches = [
                (candidate_key, providers[token])
                for candidate_key, providers in self._module_providers.items()
                if token in providers
            ]
            if len(module_matches) == 1:
                return module_matches[0]
            global_matches = [
                (candidate_key, self._module_providers[candidate_key][token])
                for candidate_key in self._global_modules
                if token in self._module_exports.get(candidate_key, set())
                and token in self._module_providers.get(candidate_key, {})
            ]
            if len(global_matches) == 1:
                return global_matches[0]
            return None, self._providers.get(token)
        module_provider = self._module_providers.get(module_key, {}).get(token)
        if module_provider is not None:
            return module_key, module_provider
        seen = seen or set()
        if module_key in seen:
            return None, None
        seen.add(module_key)
        for imported_module in self._module_imports.get(module_key, []):
            if token not in self._module_exports.get(imported_module, set()):
                continue
            owner_key, provider = self._locate_provider(token, imported_module, seen)
            if provider is not None:
                return owner_key, provider
        for global_module in self._global_modules:
            if global_module == module_key or token not in self._module_exports.get(global_module, set()):
                continue
            owner_key, provider = self._locate_provider(token, global_module, seen)
            if provider is not None:
                return owner_key, provider
        return None, self._providers.get(token)

    def _cache_key(self, module_key: Any | None, token: Any) -> Any:
        if module_key is None:
            return token
        return (module_key, token)
