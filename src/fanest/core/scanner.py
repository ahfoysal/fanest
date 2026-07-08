import inspect
from dataclasses import dataclass
from typing import Any, get_type_hints

from fanest.common.middleware import MiddlewareConsumer
from fanest.core.metadata import (
    DynamicModule,
    ExistingProvider,
    FactoryProvider,
    ForwardRef,
    InjectMarker,
    ModuleMetadata,
    ProviderDefinition,
)


@dataclass
class ModuleRecord:
    module: Any
    module_type: type
    metadata: ModuleMetadata

    @property
    def provider_tokens(self) -> set[Any]:
        return {provider_token(provider) for provider in self.metadata.providers}

    @property
    def gateway_tokens(self) -> set[Any]:
        return set(self.metadata.gateways)

    @property
    def export_tokens(self) -> set[Any]:
        return set(self.metadata.exports)

    @property
    def local_tokens(self) -> set[Any]:
        return self.provider_tokens | self.gateway_tokens


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
        self.records: dict[Any, ModuleRecord] = {}
        self._seen_modules: set[Any] = set()

    def scan(self, root_module: type) -> None:
        self._scan_module(root_module)
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

        self.records[module_key] = ModuleRecord(module=module_ref, module_type=module_type, metadata=metadata)

        for imported_module in metadata.imports:
            self._scan_module(imported_module)

        self.providers.extend(metadata.providers)
        self.providers.extend(metadata.gateways)
        self.controllers.extend(metadata.controllers)
        self.gateways.extend(metadata.gateways)
        self.middlewares.extend(metadata.middlewares)
        self.middlewares.extend(self._configured_middlewares(module_type))
        self.app_middlewares.extend(getattr(module_type, "__fanest_app_middlewares__", []))
        self.static_assets.extend(getattr(module_type, "__fanest_static_assets__", []))

    def _validate_module_boundaries(self) -> None:
        for record in self.records.values():
            visible_tokens = self._visible_tokens(record)
            for target in [*record.metadata.controllers, *record.metadata.providers, *record.metadata.gateways]:
                self._validate_target_dependencies(record, target, visible_tokens)

    def _visible_tokens(self, record: ModuleRecord) -> set[Any]:
        visible = set(record.local_tokens)
        for imported_module in record.metadata.imports:
            imported_record = self.records[self._module_key(imported_module)]
            visible.update(imported_record.export_tokens)
        for global_record in self.records.values():
            if global_record.metadata.global_module and global_record is not record:
                visible.update(global_record.export_tokens)
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
        type_hints = get_type_hints(target_type.__init__)
        for name, parameter in signature.parameters.items():
            if name == "self" or parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            default = parameter.default
            explicit_inject = isinstance(default, InjectMarker)
            dependency = default.token if explicit_inject else None
            if dependency is None:
                dependency = type_hints.get(name, parameter.annotation)
            if dependency is inspect.Parameter.empty:
                continue
            if getattr(default, "optional", False):
                continue
            if default is not inspect.Parameter.empty and not explicit_inject:
                continue
            self._validate_dependency(record, dependency, visible_tokens, target_type.__name__)

    def _validate_dependency(
        self,
        record: ModuleRecord,
        dependency: Any,
        visible_tokens: set[Any],
        target_name: Any,
    ) -> None:
        dependency = self._unwrap_token(dependency)
        if inspect.isclass(dependency) and dependency not in self._all_provider_tokens():
            return
        if dependency not in visible_tokens:
            raise TypeError(
                f"{target_name} in {record.module_type.__name__} depends on {dependency!r}, "
                "but that provider is not local or exported by an imported module."
            )

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

    def _module_key(self, module: Any, normalized: Any | None = None) -> Any:
        normalized = self._normalize_module_ref(module) if normalized is None else normalized
        if isinstance(module, dict | DynamicModule) or isinstance(normalized, DynamicModule):
            return ("dynamic", id(module if not isinstance(module, ForwardRef) else normalized))
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
        if not isinstance(module, DynamicModule):
            return base_metadata
        return ModuleMetadata(
            imports=[*base_metadata.imports, *module.imports],
            controllers=[*base_metadata.controllers, *module.controllers],
            providers=[*base_metadata.providers, *module.providers],
            gateways=[*base_metadata.gateways, *module.gateways],
            middlewares=[*base_metadata.middlewares, *module.middlewares],
            exports=[*base_metadata.exports, *module.exports],
            global_module=base_metadata.global_module or module.global_module,
        )

    def _unwrap_token(self, token: Any) -> Any:
        if isinstance(token, ForwardRef):
            return token.factory()
        return token

    def _configured_middlewares(self, module: type) -> list[Any]:
        configure = getattr(module, "configure", None)
        if configure is None:
            return []
        consumer = MiddlewareConsumer()
        instance = module()
        instance.configure(consumer)
        return consumer.middlewares
