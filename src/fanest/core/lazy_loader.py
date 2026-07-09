from typing import Any

from fanest.core.discovery import DiscoveryService
from fanest.core.module_ref import ModuleRef


class LazyModuleLoader:
    def __init__(self, container: Any) -> None:
        self.container = container

    async def load(self, module: Any) -> ModuleRef:
        from fanest.core.scanner import ModuleScanner

        scanner = ModuleScanner()
        await scanner.scan_async(module)
        return self._register(scanner)

    def load_sync(self, module: Any) -> ModuleRef:
        from fanest.core.scanner import ModuleScanner

        scanner = ModuleScanner()
        scanner.scan(module)
        return self._register(scanner)

    def _register(self, scanner: Any) -> ModuleRef:
        loaded_key: Any | None = None
        for module_key, record in scanner.records.items():
            loaded_key = loaded_key if loaded_key is not None else module_key
            imports = [scanner._module_key(imported_module) for imported_module in record.metadata.imports]
            if module_key in self.container._module_providers:
                continue
            self.container.register_module(
                module_key,
                providers=[
                    *record.metadata.providers,
                    *record.metadata.gateways,
                    *record.metadata.controllers,
                ],
                imports=imports,
                exports=scanner.export_tokens(module_key),
                global_module=record.metadata.global_module,
            )
        self._merge_discovery(scanner)
        return ModuleRef(self.container, loaded_key)

    def _merge_discovery(self, scanner: Any) -> None:
        try:
            discovery = self.container.resolve(DiscoveryService)
        except Exception:
            return
        discovery.providers.extend(scanner.providers)
        discovery.controllers.extend(scanner.controllers)
        discovery.records.update(scanner.records)
