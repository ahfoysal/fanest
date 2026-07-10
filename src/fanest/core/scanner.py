import inspect
import types
from dataclasses import dataclass
from typing import Any, get_type_hints

from fanest.common.middleware import MiddlewareConsumer, MiddlewareRoute
from fanest.core.metadata import (
    INQUIRER,
    REQUEST,
    DynamicModule,
    ExistingProvider,
    FactoryProvider,
    ForwardRef,
    InjectMarker,
    ModuleMetadata,
    ParameterSource,
    ProviderDefinition,
)
from fanest.core.module_ref import ModuleRef
from fanest.core.discovery import DiscoveryService
from fanest.core.lazy_loader import LazyModuleLoader
from fanest.core.reflector import Reflector
from fanest.schedule.registry import SchedulerRegistry
from fanest.websockets import SocketIoServer, WebSocketManager


FRAMEWORK_PROVIDER_TOKENS = {
    ModuleRef,
    LazyModuleLoader,
    DiscoveryService,
    Reflector,
    SchedulerRegistry,
    SocketIoServer,
    WebSocketManager,
    REQUEST,
    INQUIRER,
}


@dataclass
class ModuleRecord:
    module: Any
    key: Any
    module_type: type
    metadata: ModuleMetadata

    @property
    def provider_tokens(self) -> set[Any]:
        return {provider_token(provider) for provider in self.metadata.providers}

    @property
    def gateway_tokens(self) -> set[Any]:
        return set(self.metadata.gateways)

    @property
    def controller_tokens(self) -> set[Any]:
        return set(self.metadata.controllers)

    @property
    def export_tokens(self) -> set[Any]:
        return set(self.metadata.exports)

    @property
    def local_tokens(self) -> set[Any]:
        return self.provider_tokens | self.gateway_tokens | self.controller_tokens


def provider_token(provider: ProviderDefinition) -> Any:
    provide = getattr(provider, "provide", None)
    if provide is not None:
        return provide
    return provider


class ModuleScanner:
    def __init__(self) -> None:
        self.controllers: list[type] = []
        self.providers: list[ProviderDefinition] = []
        self.gateways: list[type] = []
        self.middlewares: list[type] = []
        self.app_middlewares: list[dict[str, Any]] = []
        self.static_assets: list[dict[str, str]] = []
        self.router_paths: dict[type, str] = {}
        self.records: dict[Any, ModuleRecord] = {}
        self.controller_modules: dict[type, Any] = {}
        self.gateway_modules: dict[type, Any] = {}
        self._seen_modules: set[Any] = set()

    def scan(self, root_module: type) -> None:
        self._scan_module(root_module)
        self._validate_module_boundaries()

    async def scan_async(self, root_module: Any) -> None:
        await self._scan_module_async(root_module)
        self._validate_module_boundaries()

    def _scan_module(self, module: type) -> None:
        module_ref = self._normalize_module_ref(module)
        module_key = self._module_key(module, module_ref)
        if module_key in self._seen_modules:
            return
        self._seen_modules.add(module_key)

        module_type = self._module_type(module_ref)
        metadata = self._module_metadata(module_ref)
        if metadata is None:
            raise TypeError(f"{module_type.__name__} is not a FaNest module. Add @Module(...).")

        self.records[module_key] = ModuleRecord(
            module=module_ref,
            key=module_key,
            module_type=module_type,
            metadata=metadata,
        )

        for imported_module in metadata.imports:
            self._scan_module(imported_module)

        for implicit_provider in self._module_implicit_providers(metadata, module_type):
            if implicit_provider not in metadata.providers:
                metadata.providers.append(implicit_provider)
        for provider in metadata.providers:
            provider_type = self._target_type(provider)
            if provider_type is not None and getattr(provider_type, "__fanest_gateway__", None) is not None:
                if provider_type not in metadata.gateways:
                    metadata.gateways.append(provider_type)

        self.providers.extend(metadata.providers)
        self.providers.extend(metadata.gateways)
        self.controllers.extend(metadata.controllers)
        self.gateways.extend(metadata.gateways)
        for controller in metadata.controllers:
            self.controller_modules[controller] = module_key
        for gateway in metadata.gateways:
            self.gateway_modules[gateway] = module_key
        self.middlewares.extend(metadata.middlewares)
        self.middlewares.extend(self._configured_middlewares(module_type))
        self.app_middlewares.extend(getattr(module_type, "__fanest_app_middlewares__", []))
        self.static_assets.extend(getattr(module_type, "__fanest_static_assets__", []))
        if isinstance(module_ref, DynamicModule) and module_ref.router_paths:
            self.router_paths.update(module_ref.router_paths)

    async def _scan_module_async(self, module: Any) -> None:
        module_ref = await self._normalize_module_ref_async(module)
        module_key = self._module_key(module_ref, module_ref)
        if module_key in self._seen_modules:
            return
        self._seen_modules.add(module_key)

        module_type = self._module_type(module_ref)
        metadata = self._module_metadata(module_ref)
        if metadata is None:
            raise TypeError(f"{module_type.__name__} is not a FaNest module. Add @Module(...).")

        self.records[module_key] = ModuleRecord(
            module=module_ref,
            key=module_key,
            module_type=module_type,
            metadata=metadata,
        )

        normalized_imports = []
        for imported_module in metadata.imports:
            imported_ref = await self._normalize_module_ref_async(imported_module)
            normalized_imports.append(imported_ref)
            await self._scan_module_async(imported_ref)
        metadata.imports[:] = normalized_imports

        for implicit_provider in self._module_implicit_providers(metadata, module_type):
            if implicit_provider not in metadata.providers:
                metadata.providers.append(implicit_provider)
        for provider in metadata.providers:
            provider_type = self._target_type(provider)
            if provider_type is not None and getattr(provider_type, "__fanest_gateway__", None) is not None:
                if provider_type not in metadata.gateways:
                    metadata.gateways.append(provider_type)

        self.providers.extend(metadata.providers)
        self.providers.extend(metadata.gateways)
        self.controllers.extend(metadata.controllers)
        self.gateways.extend(metadata.gateways)
        for controller in metadata.controllers:
            self.controller_modules[controller] = module_key
        for gateway in metadata.gateways:
            self.gateway_modules[gateway] = module_key
        self.middlewares.extend(metadata.middlewares)
        self.middlewares.extend(self._configured_middlewares(module_type))
        self.app_middlewares.extend(getattr(module_type, "__fanest_app_middlewares__", []))
        self.static_assets.extend(getattr(module_type, "__fanest_static_assets__", []))
        if isinstance(module_ref, DynamicModule) and module_ref.router_paths:
            self.router_paths.update(module_ref.router_paths)

    def _validate_module_boundaries(self) -> None:
        for record in self.records.values():
            self._validate_module_exports(record)
            visible_tokens = self._visible_tokens(record)
            for target in [*record.metadata.controllers, *record.metadata.providers, *record.metadata.gateways]:
                self._validate_target_dependencies(record, target, visible_tokens)

    def _validate_module_exports(self, record: ModuleRecord) -> None:
        exportable = set(record.local_tokens) | FRAMEWORK_PROVIDER_TOKENS
        for imported_module in record.metadata.imports:
            imported_record = self.records[self._module_key(imported_module)]
            exportable.update(self.export_tokens(imported_record.key))
        for exported in record.metadata.exports:
            exported_module_key = self._exported_module_key(exported)
            if exported_module_key is not None:
                continue
            token = self._unwrap_token(exported)
            token = self._resolve_named_token(token, exportable)
            if token not in exportable:
                raise TypeError(
                    f"{record.module_type.__name__} exports {token!r}, "
                    "but that provider is not local or exported by an imported module. "
                    "Register it in providers=[...] or import a module that exports it."
                )

    def _visible_tokens(self, record: ModuleRecord) -> set[Any]:
        visible = set(record.local_tokens) | FRAMEWORK_PROVIDER_TOKENS
        for imported_module in record.metadata.imports:
            imported_record = self.records[self._module_key(imported_module)]
            visible.update(self.export_tokens(imported_record.key))
        for global_record in self.records.values():
            if global_record.metadata.global_module and global_record is not record:
                visible.update(self.export_tokens(global_record.key))
        return visible

    def _validate_target_dependencies(
        self, record: ModuleRecord, target: ProviderDefinition, visible_tokens: set[Any]
    ) -> None:
        if isinstance(target, FactoryProvider):
            for dependency in target.inject:
                self._validate_dependency(record, dependency, visible_tokens, target.provide)
            return
        if isinstance(target, ExistingProvider):
            self._validate_dependency(record, target.use_existing, visible_tokens, target.provide)
            return
        target_type = self._target_type(target)
        if target_type is None:
            return
        signature = inspect.signature(target_type.__init__)
        type_hints = self._safe_type_hints(target_type.__init__)
        for name, parameter in signature.parameters.items():
            if name == "self" or parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            default = parameter.default
            explicit_inject = isinstance(default, InjectMarker)
            dependency = default.token if explicit_inject else None
            explicit_forward_ref = isinstance(default, ForwardRef)
            if explicit_forward_ref:
                dependency = default
            if dependency is None:
                dependency = type_hints.get(name, parameter.annotation)
            if dependency is inspect.Parameter.empty:
                continue
            if getattr(default, "optional", False):
                continue
            if default is not inspect.Parameter.empty and not explicit_inject and not explicit_forward_ref:
                continue
            self._validate_dependency(
                record,
                dependency,
                visible_tokens,
                target_type.__name__,
                self_only=getattr(default, "self_only", False),
                skip_self=getattr(default, "skip_self", False),
            )

    def _validate_dependency(
        self,
        record: ModuleRecord,
        dependency: Any,
        visible_tokens: set[Any],
        target_name: Any,
        *,
        self_only: bool = False,
        skip_self: bool = False,
    ) -> None:
        if isinstance(dependency, InjectMarker):
            if dependency.optional:
                return
            self_only = self_only or dependency.self_only
            skip_self = skip_self or dependency.skip_self
            dependency = dependency.token
        dependency = self._unwrap_token(dependency)
        dependency = self._resolve_named_token(dependency, visible_tokens)
        if self_only:
            visible_tokens = set(record.local_tokens) | FRAMEWORK_PROVIDER_TOKENS
        elif skip_self:
            visible_tokens = self._parent_visible_tokens(record)
        if dependency not in visible_tokens:
            raise TypeError(
                f"{target_name} in {record.module_type.__name__} depends on {dependency!r}, "
                "but that provider is not local or exported by an imported module. "
                "Register it in providers=[...] or export it from an imported module."
            )

    def _parent_visible_tokens(self, record: ModuleRecord) -> set[Any]:
        visible = set(FRAMEWORK_PROVIDER_TOKENS)
        for imported_module in record.metadata.imports:
            imported_record = self.records[self._module_key(imported_module)]
            visible.update(self.export_tokens(imported_record.key))
        for global_record in self.records.values():
            if global_record.metadata.global_module and global_record is not record:
                visible.update(self.export_tokens(global_record.key))
        return visible

    def export_tokens(self, module_key: Any, seen: set[Any] | None = None) -> set[Any]:
        seen = seen or set()
        if module_key in seen:
            return set()
        seen = {*seen, module_key}
        record = self.records[module_key]
        tokens: set[Any] = set()
        exportable = set(record.local_tokens) | FRAMEWORK_PROVIDER_TOKENS
        for imported_module in record.metadata.imports:
            imported_key = self._module_key(imported_module)
            exportable.update(self.export_tokens(imported_key, seen))
        for exported in record.metadata.exports:
            exported_module_key = self._exported_module_key(exported)
            if exported_module_key is not None:
                tokens.update(self.export_tokens(exported_module_key, seen))
                continue
            token = self._unwrap_token(exported)
            tokens.add(self._resolve_named_token(token, exportable))
        return tokens

    def _exported_module_key(self, exported: Any) -> Any | None:
        try:
            exported_ref = self._normalize_module_ref(exported)
            exported_key = self._module_key(exported_ref)
        except Exception:
            return None
        if exported_key in self.records:
            return exported_key
        exported_type = self._module_type(exported_ref) if isinstance(exported_ref, DynamicModule) else exported_ref
        for module_key, record in self.records.items():
            if record.module is exported_ref or record.module_type is exported_type:
                return module_key
        return None

    def _target_type(self, target: ProviderDefinition) -> type | None:
        use_class = getattr(target, "use_class", None)
        if use_class is not None:
            return use_class
        if inspect.isclass(target):
            return target
        return None

    def _all_provider_tokens(self) -> set[Any]:
        tokens: set[Any] = set()
        for record in self.records.values():
            tokens.update(record.local_tokens)
        return tokens

    def _normalize_module_ref(self, module: Any) -> Any:
        if isinstance(module, ForwardRef):
            return module.factory()
        if isinstance(module, dict):
            return DynamicModule(
                module=module["module"],
                imports=module.get("imports", []),
                controllers=module.get("controllers", []),
                providers=module.get("providers", []),
                gateways=module.get("gateways", []),
                middlewares=module.get("middlewares", []),
                exports=module.get("exports", []),
                global_module=module.get("global", module.get("global_module", False)),
            )
        return module

    async def _normalize_module_ref_async(self, module: Any) -> Any:
        if inspect.isawaitable(module):
            module = await module
        if isinstance(module, ForwardRef):
            module = module.factory()
            if inspect.isawaitable(module):
                module = await module
        return self._normalize_module_ref(module)

    def _module_key(self, module: Any, normalized: Any | None = None) -> Any:
        normalized = self._normalize_module_ref(module) if normalized is None else normalized
        if isinstance(normalized, DynamicModule):
            return ("dynamic", self._dynamic_module_fingerprint(normalized))
        return normalized

    def _module_type(self, module: Any) -> type:
        if isinstance(module, DynamicModule):
            return module.module
        return module

    def _module_metadata(self, module: Any) -> ModuleMetadata | None:
        module_type = self._module_type(module)
        base_metadata: ModuleMetadata | None = getattr(module_type, "__fanest_module__", None)
        if base_metadata is None:
            return None
        base_copy = ModuleMetadata(
            imports=list(base_metadata.imports),
            controllers=list(base_metadata.controllers),
            providers=list(base_metadata.providers),
            gateways=list(base_metadata.gateways),
            middlewares=list(base_metadata.middlewares),
            exports=list(base_metadata.exports),
            global_module=base_metadata.global_module,
        )
        if not isinstance(module, DynamicModule):
            return base_copy
        return ModuleMetadata(
            imports=[*base_copy.imports, *module.imports],
            controllers=[*base_copy.controllers, *module.controllers],
            providers=[*base_copy.providers, *module.providers],
            gateways=[*base_copy.gateways, *module.gateways],
            middlewares=[*base_copy.middlewares, *module.middlewares],
            exports=[*base_copy.exports, *module.exports],
            global_module=base_copy.global_module or module.global_module,
        )

    def _unwrap_token(self, token: Any) -> Any:
        if isinstance(token, ForwardRef):
            return token.factory()
        return token

    def _dynamic_module_fingerprint(self, module: DynamicModule) -> tuple[Any, ...]:
        metadata = self._module_metadata(module)
        if metadata is None:
            return ("unknown", id(module))
        return (
            self._object_fingerprint(module.module),
            tuple(self._import_fingerprint(imported) for imported in metadata.imports),
            tuple(self._object_fingerprint(controller) for controller in metadata.controllers),
            tuple(self._provider_fingerprint(provider) for provider in metadata.providers),
            tuple(self._object_fingerprint(gateway) for gateway in metadata.gateways),
            tuple(self._object_fingerprint(middleware) for middleware in metadata.middlewares),
            tuple(self._object_fingerprint(export) for export in metadata.exports),
            metadata.global_module,
            self._object_fingerprint(module.router_paths) if module.router_paths else None,
        )

    def _import_fingerprint(self, imported: Any) -> Any:
        imported = self._normalize_module_ref(imported)
        if isinstance(imported, DynamicModule):
            return ("dynamic", self._dynamic_module_fingerprint(imported))
        return self._object_fingerprint(imported)

    def _provider_fingerprint(self, provider: ProviderDefinition) -> Any:
        if isinstance(provider, ForwardRef):
            return ("forward_ref", self._object_fingerprint(provider.factory()))
        if isinstance(provider, FactoryProvider):
            return (
                "factory",
                self._object_fingerprint(provider.provide),
                self._object_fingerprint(provider.use_factory),
                tuple(self._object_fingerprint(token) for token in provider.inject),
            )
        if isinstance(provider, ExistingProvider):
            return (
                "existing",
                self._object_fingerprint(provider.provide),
                self._object_fingerprint(provider.use_existing),
            )
        use_class = getattr(provider, "use_class", None)
        if use_class is not None:
            return (
                "class_provider",
                self._object_fingerprint(provider.provide),
                self._object_fingerprint(use_class),
            )
        use_value = getattr(provider, "use_value", None)
        if hasattr(provider, "use_value"):
            return (
                "value",
                self._object_fingerprint(provider.provide),
                self._object_fingerprint(use_value),
            )
        return ("class", self._object_fingerprint(provider))

    def _object_fingerprint(self, value: Any) -> Any:
        if isinstance(value, ForwardRef):
            return ("forward_ref", self._object_fingerprint(value.factory()))
        if isinstance(value, DynamicModule):
            return ("dynamic", self._dynamic_module_fingerprint(value))
        if inspect.isclass(value):
            return ("class", value.__module__, value.__qualname__)
        if isinstance(value, (str, int, float, bool, type(None))):
            return ("literal", value)
        if isinstance(value, tuple):
            return ("tuple", tuple(self._object_fingerprint(item) for item in value))
        if isinstance(value, list):
            return ("list", tuple(self._object_fingerprint(item) for item in value))
        if isinstance(value, set):
            return ("set", tuple(sorted(repr(self._object_fingerprint(item)) for item in value)))
        if isinstance(value, dict):
            return (
                "dict",
                tuple(
                    sorted(
                        (
                            repr(self._object_fingerprint(key)),
                            self._object_fingerprint(item),
                        )
                        for key, item in value.items()
                    )
                ),
            )
        if isinstance(value, types.FunctionType):
            closure = tuple(
                self._object_fingerprint(cell.cell_contents)
                for cell in (value.__closure__ or ())
            )
            return (
                "function",
                value.__module__,
                value.__qualname__,
                value.__code__.co_code,
                value.__code__.co_consts,
                self._object_fingerprint(value.__defaults__),
                self._object_fingerprint(value.__kwdefaults__),
                closure,
            )
        return ("repr", type(value).__module__, type(value).__qualname__, repr(value))

    def _resolve_named_token(self, token: Any, candidates: set[Any]) -> Any:
        if not isinstance(token, str):
            return token
        if token in candidates:
            return token
        matches = [
            candidate
            for candidate in candidates
            if inspect.isclass(candidate) and token in {candidate.__name__, candidate.__qualname__}
        ]
        unique_matches = list(dict.fromkeys(matches))
        if len(unique_matches) == 1:
            return unique_matches[0]
        return token

    def _safe_type_hints(self, target: Any) -> dict[str, Any]:
        try:
            return get_type_hints(target)
        except Exception:
            return dict(inspect.get_annotations(target, eval_str=False))

    def _configured_middlewares(self, module: type) -> list[Any]:
        configure = getattr(module, "configure", None)
        if configure is None:
            return []
        consumer = MiddlewareConsumer()
        instance = module()
        instance.configure(consumer)
        return consumer.middlewares

    def _module_implicit_providers(self, metadata: ModuleMetadata, module_type: type) -> list[type]:
        providers: list[type] = []
        seen: set[type] = set()

        def add(component: Any) -> None:
            component = self._unwrap_token(component)
            if inspect.isclass(component) and component not in seen:
                seen.add(component)
                providers.append(component)

        for target in [*metadata.controllers, *metadata.gateways]:
            for key in (
                "__fanest_guards__",
                "__fanest_pipes__",
                "__fanest_interceptors__",
                "__fanest_filters__",
            ):
                for component in getattr(target, key, []):
                    add(component)
            for handler in self._declared_callables(target):
                for key in (
                    "__fanest_guards__",
                    "__fanest_pipes__",
                    "__fanest_interceptors__",
                    "__fanest_filters__",
                ):
                    for component in getattr(handler, key, []):
                        add(component)
                for parameter in inspect.signature(handler).parameters.values():
                    source = parameter.default
                    if isinstance(source, ParameterSource):
                        for pipe in source.pipes:
                            add(pipe)
        for middleware in [*metadata.middlewares, *self._configured_middlewares(module_type)]:
            if isinstance(middleware, MiddlewareRoute):
                add(middleware.middleware)
            else:
                add(middleware)

        return providers

    def _declared_callables(self, target: type) -> list[Any]:
        handlers: list[Any] = []
        for name, value in inspect.getmembers(target):
            if name.startswith("__"):
                continue
            candidate = value
            if isinstance(value, (staticmethod, classmethod)):
                candidate = value.__func__
            if callable(candidate):
                handlers.append(candidate)
        return handlers
