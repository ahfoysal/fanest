from pathlib import Path

from fanest import Module


class ServeStaticModule:
    @staticmethod
    def for_root(*, root_path: str, serve_root: str = "/static", name: str = "static") -> type:
        directory = str(Path(root_path))

        @Module()
        class DynamicServeStaticModule:
            pass

        setattr(
            DynamicServeStaticModule,
            "__fanest_static_assets__",
            [{"path": serve_root, "directory": directory, "name": name}],
        )
        return DynamicServeStaticModule
