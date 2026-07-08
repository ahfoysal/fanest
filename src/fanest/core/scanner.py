import inspect
from dataclasses import dataclass
from typing import Any, get_type_hints

from fanest.core.metadata import ModuleMetadata, ProviderDefinition


@dataclass
class ModuleRecord:
    module: type
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
        self.records: dict[type, ModuleRecord] = {}
        self._seen_modules: set[type] = set()

    def scan(self, root_module: type) -> None:
        self._scan_module(root_module)
        self._validate_module_boundaries()

    def _scan_module(self, module: type) -> None:
        if module in self._seen_modules:
            return
        self._seen_modules.add(module)

        metadata: ModuleMetadata | None = getattr(module, "__fanest_module__", None)
        if metadata is None:
            raise TypeError(f"{module.__name__} is not a FaNest module. Add @Module(...).")

        self.records[module] = ModuleRecord(module=module, metadata=metadata)

        for imported_module in metadata.imports:
            self._scan_module(imported_module)

        self.providers.extend(metadata.providers)
        self.providers.extend(metadata.gateways)
        self.controllers.extend(metadata.controllers)
        self.gateways.extend(metadata.gateways)
        self.middlewares.extend(metadata.middlewares)

    def _validate_module_boundaries(self) -> None:
        for record in self.records.values():
            visible_tokens = self._visible_tokens(record)
            for target in [*record.metadata.controllers, *record.metadata.providers, *record.metadata.gateways]:
                self._validate_target_dependencies(record, target, visible_tokens)

    def _visible_tokens(self, record: ModuleRecord) -> set[Any]:
        visible = set(record.local_tokens)
        for imported_module in record.metadata.imports:
            imported_record = self.records[imported_module]
            visible.update(imported_record.export_tokens)
        return visible

    def _validate_target_dependencies(
        self, record: ModuleRecord, target: ProviderDefinition, visible_tokens: set[Any]
    ) -> None:
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
            dependency = getattr(default, "token", None)
            if dependency is None:
                dependency = type_hints.get(name, parameter.annotation)
            if dependency is inspect.Parameter.empty:
                continue
            if getattr(default, "optional", False):
                continue
            if inspect.isclass(dependency) and dependency not in self._all_provider_tokens():
                continue
            if dependency not in visible_tokens:
                raise TypeError(
                    f"{target_type.__name__} in {record.module.__name__} depends on {dependency!r}, "
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
