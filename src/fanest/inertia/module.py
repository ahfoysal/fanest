"""Inertia.js server-side adapter for FaNest.

An opt-in module that lets FaNest drive Inertia-powered single-page apps
(React / Vue / Svelte via Vite) exactly like Laravel's ``inertia-laravel`` —
covering the full Inertia protocol (page object, partial reloads, asset
versioning, 303 redirects, external ``location`` redirects), the v2 features
(deferred props, merge props, history encryption), first-class Vite asset
injection (HMR in dev, manifest in prod, React Refresh), and optional SSR.

Usage::

    @Module(imports=[InertiaModule.for_root(
        vite={"dev_server": "http://localhost:5173", "entrypoints": ["src/main.tsx"],
              "manifest": "public/build/.vite/manifest.json"},
        version="1.0",
        share=lambda request: {"auth": {"user": getattr(request.state, "user", None)}},
    )])
    class AppModule: ...

    @Controller("users")
    class UsersController:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/")
        async def index(self):
            return await self.inertia.render("Users/Index", {"users": [...]})
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from fanest import Controller, Get, Module, use_value

from fanest.inertia.context import HandleInertiaRequests, INERTIA_OPTIONS, InertiaConfig
from fanest.inertia.errors import ExceptionResponse, InertiaExceptionFilter
from fanest.inertia.middleware import (
    EncryptHistoryMiddleware,
    InertiaCsrfMiddleware,
    InertiaMiddleware,
    MethodOverrideMiddleware,
)
from fanest.inertia.props import (
    AlwaysProp,
    DeferProp,
    LazyProp,
    MergeProp,
    OnceProp,
    OptionalProp,
    ScrollProp,
)
from fanest.inertia.rendering import InertiaComponentNotFoundError, _ensure_component_exists
from fanest.inertia.service import InertiaResponseBuilder, InertiaService
from fanest.inertia.ssr import InertiaSSR
from fanest.inertia.vite import ViteAssets

__all__ = [
    "AlwaysProp",
    "DeferProp",
    "EncryptHistoryMiddleware",
    "ExceptionResponse",
    "HandleInertiaRequests",
    "InertiaComponentNotFoundError",
    "InertiaConfig",
    "InertiaCsrfMiddleware",
    "InertiaExceptionFilter",
    "InertiaMiddleware",
    "InertiaModule",
    "InertiaResponseBuilder",
    "InertiaSSR",
    "InertiaService",
    "LazyProp",
    "MergeProp",
    "MethodOverrideMiddleware",
    "OnceProp",
    "OptionalProp",
    "ScrollProp",
    "ViteAssets",
    "inertia_route",
    "_ensure_component_exists",
]


# --------------------------------------------------------------------------- #
# Module
# --------------------------------------------------------------------------- #
class InertiaModule:
    @staticmethod
    def for_root(
        *,
        root_view: str | Callable[[Request], str] = "app",
        root_element: str = "app",
        version: str | bool | Callable[..., str | bool | None] | None = None,
        template: str | Callable[..., str] | None = None,
        share: Callable[[Request], dict[str, Any]] | dict[str, Any] | None = None,
        encrypt_history: bool | Callable[[], bool] = False,
        ssr: dict[str, Any] | bool | None = None,
        vite: dict[str, Any] | None = None,
        transform_component: Callable[[str], str] | None = None,
        resolve_url: Callable[[Request], str] | None = None,
        with_all_errors: bool = False,
        ensure_pages_exist: bool = False,
        page_paths: list[str] | None = None,
        page_extensions: list[str] | None = None,
        error_component: str = "Error",
        error_statuses: tuple[int, ...] = (403, 404, 500, 503),
        debug: bool = False,
        preserve_fragment: bool = True,
        handler: "type[HandleInertiaRequests] | HandleInertiaRequests | None" = None,
        encrypt_history_routes: list[str] | None = None,
        csrf: bool | dict[str, Any] = False,
        method_override: bool = True,
        is_global: bool = True,
    ) -> type:
        # An object-oriented HandleInertiaRequests handler supplies the request-aware
        # version/share/root_view/encrypt_history; explicit kwargs still win over it.
        instance = handler() if isinstance(handler, type) else handler
        if instance is not None:
            if version is None:
                version = instance.version
            if share is None:
                share = instance.share
            if root_view == "app":
                root_view = instance.root_view
            if encrypt_history is False:
                encrypt_history = instance.encrypt_history

        options: dict[str, Any] = {
            "root_view": root_view,
            "root_element": root_element,
            "version": version,
            "template": template,
            "share": share,
            "encrypt_history": encrypt_history,
            "ssr": ssr,
            "vite": vite,
            "transform_component": transform_component,
            "resolve_url": resolve_url,
            "with_all_errors": with_all_errors,
            "ensure_pages_exist": ensure_pages_exist,
            "error_component": error_component,
            "error_statuses": error_statuses,
            "debug": debug,
            "preserve_fragment": preserve_fragment,
        }
        if page_paths is not None:
            options["page_paths"] = page_paths
        if page_extensions is not None:
            options["page_extensions"] = page_extensions

        @Module(
            providers=[use_value(INERTIA_OPTIONS, options), InertiaService],
            exports=[InertiaService],
            global_module=is_global,
        )
        class DynamicInertiaModule:
            pass

        # innermost first: InertiaMiddleware (sets context nearest the handler),
        # then CSRF, then method-override (outermost, rewrites the verb first).
        middlewares: list[dict[str, Any]] = [
            {"class": InertiaMiddleware, "options": {"config": InertiaConfig(**options)}}
        ]
        if csrf:
            middlewares.append({"class": InertiaCsrfMiddleware, "options": csrf if isinstance(csrf, dict) else {}})
        if method_override:
            middlewares.append({"class": MethodOverrideMiddleware, "options": {}})
        # EncryptHistory must run *inside* InertiaMiddleware (it mutates the state
        # the middleware just seeded), so prepend it as the new innermost layer.
        if encrypt_history_routes:
            middlewares.insert(
                0, {"class": EncryptHistoryMiddleware, "options": {"paths": list(encrypt_history_routes)}}
            )
        setattr(DynamicInertiaModule, "__fanest_app_middlewares__", middlewares)
        return DynamicInertiaModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]],
        inject: list[Any] | None = None,
        is_global: bool = True,
    ) -> type:
        from fanest.core.providers import use_factory as provider_factory

        @Module(
            providers=[provider_factory(INERTIA_OPTIONS, use_factory, inject=inject or []), InertiaService],
            exports=[InertiaService],
            global_module=is_global,
        )
        class DynamicInertiaModule:
            pass

        return DynamicInertiaModule


def inertia_route(path: str, component: str, props: dict[str, Any] | None = None) -> type:
    """Bind a URL directly to an Inertia component render, no explicit handler —
    the equivalent of Laravel's ``Route::inertia('/about', 'About')``. Returns a
    controller to list in a module's ``controllers=[...]``.

        @Module(controllers=[inertia_route("/about", "About", {"team": TEAM})])
        class AppModule: ...
    """
    resolved_props = dict(props or {})

    @Controller("")
    class _InertiaRouteController:
        def __init__(self, inertia: InertiaService) -> None:
            self.inertia = inertia

        @Get(path)
        async def _render(self) -> Response:
            return await self.inertia.render(component, dict(resolved_props))

    _InertiaRouteController.__name__ = f"InertiaRoute_{component.replace('/', '_')}"
    return _InertiaRouteController
