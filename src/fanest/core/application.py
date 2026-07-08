from typing import Any

from fastapi import FastAPI


class FaNestApplication:
    def __init__(
        self,
        root_module: type,
        *,
        title: str = "FaNest Application",
        version: str = "0.1.0",
        description: str | None = None,
        debug: bool = False,
    ) -> None:
        self.root_module = root_module
        self.options: dict[str, Any] = {
            "title": title,
            "version": version,
            "description": description,
            "debug": debug,
        }
        self.global_guards: list[Any] = []
        self.global_pipes: list[Any] = []
        self.global_interceptors: list[Any] = []
        self.global_filters: list[Any] = []
        self._app: FastAPI | None = None

    def set_global_prefix(self, prefix: str) -> "FaNestApplication":
        self._ensure_not_built()
        self.options["global_prefix"] = prefix
        return self

    def enable_cors(self, options: dict[str, Any] | bool = True) -> "FaNestApplication":
        self._ensure_not_built()
        self.options["cors"] = options
        return self

    def enable_compression(self, *, minimum_size: int = 500) -> "FaNestApplication":
        from fanest.platform_fastapi.modules import enable_compression

        enable_compression(self.build(), minimum_size=minimum_size)
        return self

    def serve_static(self, path: str, directory: str, *, name: str = "static") -> "FaNestApplication":
        from fanest.platform_fastapi.modules import serve_static

        serve_static(self.build(), path, directory, name=name)
        return self

    def use_global_guards(self, *guards: Any) -> "FaNestApplication":
        self._ensure_not_built()
        self.global_guards.extend(guards)
        return self

    def use_global_pipes(self, *pipes: Any) -> "FaNestApplication":
        self._ensure_not_built()
        self.global_pipes.extend(pipes)
        return self

    def use_global_interceptors(self, *interceptors: Any) -> "FaNestApplication":
        self._ensure_not_built()
        self.global_interceptors.extend(interceptors)
        return self

    def use_global_filters(self, *filters: Any) -> "FaNestApplication":
        self._ensure_not_built()
        self.global_filters.extend(filters)
        return self

    def build(self) -> FastAPI:
        if self._app is None:
            from fanest.core.factory import FaNestFactory

            self._app = FaNestFactory.create(
                self.root_module,
                **self.options,
                global_guards=self.global_guards,
                global_pipes=self.global_pipes,
                global_interceptors=self.global_interceptors,
                global_filters=self.global_filters,
            )
        return self._app

    @property
    def fastapi(self) -> FastAPI:
        return self.build()

    async def __call__(self, scope, receive, send):
        await self.build()(scope, receive, send)

    def listen(self, *, host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
        import uvicorn

        uvicorn.run(self.build(), host=host, port=port, reload=reload)

    def _ensure_not_built(self) -> None:
        if self._app is not None:
            raise RuntimeError("FaNestApplication options cannot be changed after build().")
