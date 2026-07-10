"""Hierarchical route registration, mirroring Nest's ``RouterModule``.

``RouterModule.register`` assigns URL prefixes to modules; every controller
declared by a listed module (and its children, whose prefixes nest under the
parent's) is served under that prefix. The routed modules must still be
imported into the application's module tree as usual::

    @Module(imports=[
        AdminModule,
        MetricsModule,
        RouterModule.register([
            {"path": "admin", "module": AdminModule, "children": [
                {"path": "metrics", "module": MetricsModule},
            ]},
        ]),
    ])
    class AppModule: ...
"""

from typing import Any

from fanest.core.metadata import DynamicModule
from fanest.core.module import Module


@Module()
class RouterModule:
    @staticmethod
    def register(routes: list[Any]) -> DynamicModule:
        paths: dict[type, str] = {}
        RouterModule._collect(routes, "", paths)
        return DynamicModule(module=RouterModule, router_paths=paths)

    @staticmethod
    def _collect(entries: list[Any], parent: str, paths: dict[type, str]) -> None:
        for entry in entries:
            if isinstance(entry, dict):
                path = str(entry.get("path") or "")
                module = entry.get("module")
                children = entry.get("children") or []
            else:
                path, module, children = "", entry, []
            prefix = RouterModule._join(parent, path)
            if module is not None:
                module_type = module.module if isinstance(module, DynamicModule) else module
                if not isinstance(module_type, type):
                    raise TypeError(
                        f"RouterModule route 'module' must be a module class, got {module_type!r}."
                    )
                paths[module_type] = prefix
            elif not children:
                raise ValueError(
                    "RouterModule route entries need a 'module', 'children', or both."
                )
            RouterModule._collect(children, prefix, paths)

    @staticmethod
    def _join(parent: str, path: str) -> str:
        segments = [segment for segment in (*parent.split("/"), *path.split("/")) if segment]
        return "/".join(segments)
