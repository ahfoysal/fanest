from typing import Any, Protocol

from fanest._version import __version__ as _DEFAULT_FANEST_VERSION
from fanest.common.versioning import VersioningOptions
from fastapi import FastAPI


class HttpAdapterProtocol(Protocol):
    """Public HTTP adapter surface exposed by FaNestApplication.

    Platform adapters own controller and gateway registration for one ASGI app.
    Applications that need multiple HTTP server processes should build the ASGI
    app once and hand it to their server runner, or implement this adapter
    protocol for another platform.
    """

    app: FastAPI

    def register_controllers(self, controllers: list[type]) -> None: ...

    def register_gateways(self, gateways: list[type]) -> None: ...


class FaNestApplication:
    """Configurable FaNest application wrapper around a single ASGI app.

    The wrapper mirrors Nest-style application setup while keeping FastAPI as
    the default HTTP platform. Options must be configured before build(), after
    which get_http_adapter(), fastapi, serverless_handler(), and listen() all
    operate on the same ASGI application instance.
    """

    def __init__(
        self,
        root_module: type,
        *,
        title: str = "FaNest Application",
        version: str | None = None,
        description: str | None = None,
        debug: bool = False,
        global_prefix: str = "",
        cors: dict[str, Any] | bool = False,
        raw_body: bool = False,
        versioning: VersioningOptions | dict[str, Any] | bool | None = None,
    ) -> None:
        if version is None:
            version = _DEFAULT_FANEST_VERSION
        self.root_module = root_module
        self.options: dict[str, Any] = {
            "title": title,
            "version": version,
            "description": description,
            "debug": debug,
            "global_prefix": global_prefix,
            "cors": cors,
            "raw_body": raw_body,
            "versioning": versioning,
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

    def enable_raw_body(self) -> "FaNestApplication":
        self._ensure_not_built()
        self.options["raw_body"] = True
        return self

    def enable_versioning(
        self,
        options: VersioningOptions | dict[str, Any] | bool = True,
    ) -> "FaNestApplication":
        self._ensure_not_built()
        self.options["versioning"] = options
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

    async def build_async(self) -> FastAPI:
        if self._app is None:
            from fanest.core.factory import FaNestFactory

            self._app = await FaNestFactory.create_async(
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
        """Return the underlying FastAPI ASGI application, building it once."""

        return self.build()

    def get_http_adapter(self) -> HttpAdapterProtocol:
        """Return the active platform adapter.

        The adapter contract is intentionally small: it exposes the ASGI app and
        controller/gateway registration methods. Use build()/fastapi to mount
        or run the app with any ASGI server, including multi-process or
        multi-listener server setups managed outside FaNest.
        """

        return self.build().state.fanest_http_adapter

    def serverless_handler(self) -> FastAPI:
        """Return a stable ASGI callable for serverless adapters.

        Serverless platforms can import this value directly. Repeated calls
        return the same built ASGI app, preserving startup/lifespan behavior
        expected by the platform runtime.
        """

        return self.build()

    async def __call__(self, scope, receive, send):
        await self.build()(scope, receive, send)

    def listen(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        reload: bool = False,
        ssl_keyfile: str | None = None,
        ssl_certfile: str | None = None,
        timeout_keep_alive: int = 5,
        **uvicorn_options: Any,
    ) -> None:
        """Run the ASGI app with uvicorn on one HTTP listener.

        HTTPS and keep-alive options are forwarded to uvicorn. Multi-server or
        multi-listener topologies should run build()/fastapi with the ASGI
        server's own process manager instead of passing a servers list here.
        """

        import uvicorn

        if "servers" in uvicorn_options:
            raise NotImplementedError(
                "FaNestApplication.listen() starts one HTTP server. "
                "Create multiple server processes with your ASGI server or call build() and mount the app yourself."
            )

        uvicorn.run(
            self.build(),
            host=host,
            port=port,
            reload=reload,
            ssl_keyfile=ssl_keyfile,
            ssl_certfile=ssl_certfile,
            timeout_keep_alive=timeout_keep_alive,
            **uvicorn_options,
        )

    def _ensure_not_built(self) -> None:
        if self._app is not None:
            raise RuntimeError("FaNestApplication options cannot be changed after build().")
