from fanest.core.metadata import ModuleMetadata, ProviderDefinition


class ModuleScanner:
    def __init__(self) -> None:
        self.controllers: list[type] = []
        self.providers: list[ProviderDefinition] = []
        self.gateways: list[type] = []
        self._seen_modules: set[type] = set()

    def scan(self, root_module: type) -> None:
        self._scan_module(root_module)

    def _scan_module(self, module: type) -> None:
        if module in self._seen_modules:
            return
        self._seen_modules.add(module)

        metadata: ModuleMetadata | None = getattr(module, "__fanest_module__", None)
        if metadata is None:
            raise TypeError(f"{module.__name__} is not a FaNest module. Add @Module(...).")

        for imported_module in metadata.imports:
            self._scan_module(imported_module)

        self.providers.extend(metadata.providers)
        self.providers.extend(metadata.gateways)
        self.controllers.extend(metadata.controllers)
        self.gateways.extend(metadata.gateways)
