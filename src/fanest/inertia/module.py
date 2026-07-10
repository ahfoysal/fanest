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

import html
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token

INERTIA_OPTIONS = token("INERTIA_OPTIONS")

#: Session key holding data flashed for exactly one follow-up request.
_FLASH_KEY = "_inertia_flash"


# --------------------------------------------------------------------------- #
# Prop wrappers (mirror Inertia::lazy / always / defer / merge / optional)
# --------------------------------------------------------------------------- #
class IgnoreOnFirstLoad:
    """Marker: props that are never sent on a full page visit (only on partials)."""


@dataclass
class LazyProp(IgnoreOnFirstLoad):
    """Evaluated only when explicitly requested via a partial reload."""

    callback: Callable[[], Any]


# ``optional`` is the Inertia v2 name for the same behaviour.
OptionalProp = LazyProp


@dataclass
class AlwaysProp:
    """Always included, even in partial reloads that don't request it."""

    value: Any


@dataclass
class DeferProp(IgnoreOnFirstLoad):
    """Excluded on first load; the client auto-fetches it after mount. Its key is
    advertised under ``deferredProps`` on the initial page object."""

    callback: Callable[[], Any]
    group: str = "default"
    merge: bool = False


@dataclass
class MergeProp:
    """Included normally, but the client merges (instead of replaces) it — its key
    is advertised under ``mergeProps`` (shallow) or ``deepMergeProps`` (deep), for
    Inertia v2 infinite scroll / append. ``match_on`` fields are advertised under
    ``matchPropsOn`` so the client matches array items by key instead of index."""

    value: Any
    deep: bool = False
    match_on: list[str] | None = None


# --------------------------------------------------------------------------- #
# Per-request state (set by the middleware, read by InertiaService)
# --------------------------------------------------------------------------- #
@dataclass
class _InertiaState:
    request: Request
    shared: dict[str, Any] = field(default_factory=dict)
    version: str | None = None
    encrypt_history: bool = False
    clear_history: bool = False
    flash: dict[str, Any] = field(default_factory=dict)
    flash_consumed: bool = False


def _consume_flash(state: "_InertiaState") -> None:
    """Pop session flash data once per request. Lazy so it works no matter how
    the session middleware is ordered relative to InertiaMiddleware."""
    if state.flash_consumed:
        return
    session = state.request.scope.get("session")
    if not isinstance(session, dict):
        return
    state.flash_consumed = True
    popped = session.pop(_FLASH_KEY, None)
    if isinstance(popped, dict):
        state.flash = popped
        if popped.get("errors"):
            state.shared["errors"] = popped["errors"]


_current: ContextVar[_InertiaState | None] = ContextVar("fanest_inertia_state", default=None)


@dataclass
class InertiaConfig:
    root_view: str = "app"
    root_element: str = "app"
    version: str | Callable[[], str] | None = None
    template: str | Callable[["ViteAssets", str, str], str] | None = None
    share: Callable[[Request], dict[str, Any]] | dict[str, Any] | None = None
    encrypt_history: bool = False
    ssr: dict[str, Any] | bool | None = None
    vite: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Vite integration (@vite: dev HMR client + entrypoints, or prod manifest)
# --------------------------------------------------------------------------- #
class ViteAssets:
    def __init__(self, options: dict[str, Any] | None) -> None:
        options = options or {}
        self.dev_server: str | None = options.get("dev_server")
        self.entrypoints: list[str] = list(options.get("entrypoints", options.get("input", [])) or [])
        self.manifest_path: str | None = options.get("manifest")
        self.hot_file: str | None = options.get("hot_file")
        self.build_directory: str = options.get("build_directory", "build")
        self.react_refresh: bool = options.get("react_refresh", True)
        self._manifest: dict[str, Any] | None = None

    def is_dev(self) -> bool:
        if self.hot_file and Path(self.hot_file).exists():
            return True
        if self.manifest_path and Path(self.manifest_path).exists():
            return False
        return bool(self.dev_server)

    def _dev_url(self) -> str:
        if self.hot_file and Path(self.hot_file).exists():
            return Path(self.hot_file).read_text(encoding="utf-8").strip().rstrip("/")
        return (self.dev_server or "http://localhost:5173").rstrip("/")

    def _manifest_data(self) -> dict[str, Any]:
        manifest = self._manifest
        if manifest is None:
            if not self.manifest_path or not Path(self.manifest_path).exists():
                manifest = {}
            else:
                manifest = json.loads(Path(self.manifest_path).read_text(encoding="utf-8"))
            self._manifest = manifest
        return manifest

    def version_hash(self) -> str:
        """A content hash of the Vite manifest, so a rebuild busts the client cache."""
        if self.manifest_path and Path(self.manifest_path).exists():
            import hashlib

            return hashlib.md5(Path(self.manifest_path).read_bytes()).hexdigest()[:12]
        if self.hot_file and Path(self.hot_file).exists():
            return "dev"
        return ""

    def tags(self) -> str:
        if not self.entrypoints:
            return ""
        if self.is_dev():
            base = self._dev_url()
            tags = [f'<script type="module" src="{base}/@vite/client"></script>']
            if self.react_refresh:
                tags.append(
                    f'<script type="module">'
                    f'import RefreshRuntime from "{base}/@react-refresh";'
                    f"RefreshRuntime.injectIntoGlobalHook(window);"
                    f"window.$RefreshReg$=()=>{{}};window.$RefreshSig$=()=>(type)=>type;"
                    f"window.__vite_plugin_react_preamble_installed__=true;</script>"
                )
            for entry in self.entrypoints:
                tags.append(f'<script type="module" src="{base}/{entry}"></script>')
            return "\n".join(tags)
        # production: resolve entrypoints through the manifest
        manifest = self._manifest_data()
        base = f"/{self.build_directory.strip('/')}"
        tags = []
        seen_css: set[str] = set()
        for entry in self.entrypoints:
            chunk = manifest.get(entry)
            if chunk is None:
                continue
            for css in chunk.get("css", []):
                if css not in seen_css:
                    seen_css.add(css)
                    tags.append(f'<link rel="stylesheet" href="{base}/{css}">')
            for imported in chunk.get("imports", []):
                imported_chunk = manifest.get(imported, {})
                for css in imported_chunk.get("css", []):
                    if css not in seen_css:
                        seen_css.add(css)
                        tags.append(f'<link rel="stylesheet" href="{base}/{css}">')
            tags.append(f'<script type="module" src="{base}/{chunk["file"]}"></script>')
        return "\n".join(tags)


# --------------------------------------------------------------------------- #
# SSR client (POST the page object to the Node render server)
# --------------------------------------------------------------------------- #
class InertiaSSR:
    def __init__(self, options: dict[str, Any] | bool | None) -> None:
        if options in (None, False):
            self.enabled = False
            self.url = ""
            return
        if options is True:
            options = {}
        self.enabled = bool(options.get("enabled", True))
        self.url = str(options.get("url", "http://127.0.0.1:13714")).rstrip("/")

    async def render(self, page: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(f"{self.url}/render", json=page)
                response.raise_for_status()
                return response.json()
        except Exception:
            # Graceful fallback to client-side rendering if the SSR server is down.
            return None


# --------------------------------------------------------------------------- #
# Prop resolution (partial reloads, lazy/always/defer/merge)
# --------------------------------------------------------------------------- #
async def _evaluate(value: Any) -> Any:
    if isinstance(value, (LazyProp, DeferProp)):
        value = value.callback
    elif isinstance(value, AlwaysProp):
        value = value.value
    elif isinstance(value, MergeProp):
        value = value.value
    if callable(value):
        value = value()
    if hasattr(value, "__await__"):
        value = await value
    return value


def _pick_paths(value: Any, subpaths: list[str]) -> Any:
    """Keep only the given dot-paths within a nested dict (Inertia dot-notation)."""
    if not isinstance(value, dict):
        return value
    groups: dict[str, list[str]] = {}
    for sub in subpaths:
        head, _, tail = sub.partition(".")
        groups.setdefault(head, [])
        if tail:
            groups[head].append(tail)
    result: dict[str, Any] = {}
    for head, tails in groups.items():
        if head in value:
            result[head] = _pick_paths(value[head], tails) if tails else value[head]
    return result


def _apply_only_paths(resolved: dict[str, Any], only: list[str]) -> dict[str, Any]:
    whole: set[str] = {p for p in only if "." not in p}
    nested: dict[str, list[str]] = {}
    for p in only:
        head, _, tail = p.partition(".")
        if tail:
            nested.setdefault(head, []).append(tail)
    result: dict[str, Any] = {}
    for key, value in resolved.items():
        if key in whole:
            result[key] = value
        elif key in nested:
            result[key] = _pick_paths(value, nested[key])
    return result


def _forget_paths(value: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    result = dict(value)
    nested: dict[str, list[str]] = {}
    for p in paths:
        head, _, tail = p.partition(".")
        if tail:
            nested.setdefault(head, []).append(tail)
        else:
            result.pop(head, None)
    for head, tails in nested.items():
        if isinstance(result.get(head), dict):
            result[head] = _forget_paths(result[head], tails)
    return result


async def _resolve_props(
    props: dict[str, Any],
    *,
    component: str,
    request: Request,
) -> tuple[dict[str, Any], dict[str, list[str]], list[str], list[str], list[str]]:
    partial_component = request.headers.get("x-inertia-partial-component")
    is_partial = partial_component == component
    # An absent/empty Partial-Data header means "no only-filter" (Laravel only
    # applies the filter when the list is non-empty), so except-only partial
    # reloads still receive every non-excepted prop.
    only = (_split_header(request.headers.get("x-inertia-partial-data")) or None) if is_partial else None
    excepted = _split_header(request.headers.get("x-inertia-partial-except")) if is_partial else []
    reset = set(_split_header(request.headers.get("x-inertia-reset")))
    only_top = {p.split(".", 1)[0] for p in only} if only is not None else None
    except_top = {p for p in excepted if "." not in p}

    resolved: dict[str, Any] = {}
    always_props: dict[str, Any] = {}
    deferred: dict[str, list[str]] = {}
    merge_keys: list[str] = []
    deep_merge_keys: list[str] = []
    match_on: list[str] = []

    for key, value in props.items():
        # `always` props (and the shared `errors` bag) appear on every response,
        # so they bypass only/except filtering and are re-merged at the end.
        if isinstance(value, AlwaysProp) or key == "errors":
            always_props[key] = await _evaluate(value)
            continue
        if is_partial:
            if only_top is not None and key not in only_top:
                continue
            if key in except_top:
                continue
        elif isinstance(value, IgnoreOnFirstLoad):
            if isinstance(value, DeferProp):
                deferred.setdefault(value.group, []).append(key)
            continue
        if key not in reset:  # X-Inertia-Reset -> client replaces instead of merges
            if isinstance(value, MergeProp):
                (deep_merge_keys if value.deep else merge_keys).append(key)
                for field_name in value.match_on or []:
                    match_on.append(f"{key}.{field_name}")
            elif isinstance(value, DeferProp) and value.merge:
                merge_keys.append(key)
        resolved[key] = await _evaluate(value)

    # dot-notation: build the `only` subset, then forget dotted `except` from it
    if only is not None and any("." in p for p in only):
        resolved = _apply_only_paths(resolved, only)
    dotted_except = [p for p in excepted if "." in p]
    if dotted_except:
        resolved = _forget_paths(resolved, dotted_except)

    resolved.update(always_props)
    return resolved, deferred, merge_keys, deep_merge_keys, match_on


def _split_header(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


# --------------------------------------------------------------------------- #
# Page building + response rendering
# --------------------------------------------------------------------------- #
def _resolve_version(config: InertiaConfig, state: _InertiaState) -> str:
    if state.version is not None:
        return state.version
    version = config.version
    if callable(version):
        version = version()
    if version is None:
        # Laravel default: hash the Vite manifest so a rebuild busts the client cache.
        return ViteAssets(config.vite).version_hash()
    return str(version)


def _default_template(vite: ViteAssets, head: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{vite.tags()}\n{head}\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>"
    )


async def _render_response(config: InertiaConfig, state: _InertiaState, component: str, props: dict[str, Any]) -> Response:
    request = state.request
    _consume_flash(state)
    # shared data is merged under the page props; explicit props win on key clash
    merged = {**state.shared, **props}
    resolved, deferred, merge_keys, deep_merge_keys, match_on = await _resolve_props(
        merged, component=component, request=request
    )

    # `errors` is always present; nest it under the error bag when one is requested
    errors = resolved.get("errors", {})
    error_bag = request.headers.get("x-inertia-error-bag")
    if error_bag and isinstance(errors, dict) and errors and error_bag not in errors:
        errors = {error_bag: errors}
    resolved["errors"] = errors

    page: dict[str, Any] = {
        "component": component,
        "props": resolved,
        "url": request.url.path + (("?" + request.url.query) if request.url.query else ""),
        "version": _resolve_version(config, state),
        # history booleans are always emitted (matches inertia-laravel)
        "clearHistory": state.clear_history,
        "encryptHistory": state.encrypt_history or config.encrypt_history,
    }
    if deferred:
        page["deferredProps"] = deferred
    if merge_keys:
        page["mergeProps"] = merge_keys
    if deep_merge_keys:
        page["deepMergeProps"] = deep_merge_keys
    if match_on:
        page["matchPropsOn"] = match_on

    # X-Inertia visit -> JSON page object
    if request.headers.get("x-inertia"):
        return JSONResponse(page, headers={"X-Inertia": "true", "Vary": "X-Inertia"})

    # First visit -> full HTML document (optionally server-side rendered)
    vite = ViteAssets(config.vite)
    ssr_result = None
    ssr = InertiaSSR(config.ssr)
    if ssr.enabled:
        ssr_result = await ssr.render(page)

    if ssr_result is not None:
        head_fragments = ssr_result.get("head", [])
        head = "\n".join(head_fragments) if isinstance(head_fragments, list) else str(head_fragments)
        body = ssr_result.get("body", "")
    else:
        encoded = html.escape(json.dumps(page, separators=(",", ":"), default=str), quote=True)
        head = ""
        body = f'<div id="{html.escape(config.root_element, quote=True)}" data-page="{encoded}"></div>'

    template = config.template or _default_template
    if callable(template):
        document = template(vite, head, body)
    else:
        document = str(template).replace("@inertiaHead", f"{vite.tags()}\n{head}").replace("@inertia", body)
    return HTMLResponse(document, headers={"Vary": "X-Inertia"})


# --------------------------------------------------------------------------- #
# Public service
# --------------------------------------------------------------------------- #
def _state() -> _InertiaState:
    state = _current.get()
    if state is None:
        raise RuntimeError(
            "Inertia is not active for this request. Ensure InertiaModule.for_root(...) is imported "
            "so InertiaMiddleware runs."
        )
    return state


@Injectable()
class InertiaService:
    def __init__(self, options: dict[str, Any] = Inject(INERTIA_OPTIONS)):
        self.config = InertiaConfig(**(options or {}))

    async def render(self, component: str, props: dict[str, Any] | None = None) -> Response:
        return await _render_response(self.config, _state(), component, props or {})

    def share(self, key: str | dict[str, Any], value: Any = None) -> "InertiaService":
        shared = _state().shared
        if isinstance(key, dict):
            shared.update(key)
        else:
            shared[key] = value
        return self

    def get_shared(self, key: str | None = None) -> Any:
        shared = _state().shared
        return shared if key is None else shared.get(key)

    def location(self, url: str) -> Response:
        request = _current.get()
        if request is not None and request.request.headers.get("x-inertia"):
            return Response(status_code=409, headers={"X-Inertia-Location": url})
        return Response(status_code=302, headers={"Location": url})

    def back(self, fallback: str = "/", status: int | None = None) -> Response:
        """Redirect to the Referer (Laravel ``back()``). PUT/PATCH/DELETE get a
        303 so the browser re-issues the follow-up visit as GET."""
        request = _state().request
        url = request.headers.get("referer") or fallback
        if status is None:
            status = 303 if request.method in {"PUT", "PATCH", "DELETE"} else 302
        return Response(status_code=status, headers={"Location": url, "Vary": "X-Inertia"})

    def flash(self, key: str | dict[str, Any], value: Any = None) -> "InertiaService":
        """Stash data in the session for exactly the next request (Laravel
        session flash). Requires SessionModule."""
        # Consume the incoming flash first so it cannot swallow what we are
        # about to stash for the next request.
        _consume_flash(_state())
        session = self._session()
        bucket = session.setdefault(_FLASH_KEY, {})
        if isinstance(key, dict):
            bucket.update(key)
        else:
            bucket[key] = value
        return self

    def get_flash(self, key: str | None = None, default: Any = None) -> Any:
        """Read data flashed by the previous request."""
        state = _state()
        _consume_flash(state)
        return state.flash if key is None else state.flash.get(key, default)

    def with_errors(
        self,
        errors: dict[str, Any],
        *,
        error_bag: str | None = None,
        fallback: str = "/",
    ) -> Response:
        """Flash validation errors and redirect back — the Laravel
        redirect-back-with-errors flow. On the next request the errors are
        automatically shared as the ``errors`` prop (nested under the bag when
        one is given)."""
        payload: dict[str, Any] = {error_bag: errors} if error_bag else errors
        self.flash("errors", payload)
        return self.back(fallback=fallback)

    def _session(self) -> dict[str, Any]:
        session = _state().request.scope.get("session")
        if not isinstance(session, dict):
            raise RuntimeError(
                "Session-backed Inertia features (flash / with_errors) require "
                "SessionModule.for_root(...) to be imported."
            )
        return session

    def set_version(self, version: str) -> "InertiaService":
        _state().version = version
        return self

    def encrypt_history(self, encrypt: bool = True) -> "InertiaService":
        _state().encrypt_history = encrypt
        return self

    def clear_history(self, clear: bool = True) -> "InertiaService":
        _state().clear_history = clear
        return self

    # prop factories (Inertia::lazy / optional / always / defer / merge)
    @staticmethod
    def lazy(callback: Callable[[], Any]) -> LazyProp:
        return LazyProp(callback)

    optional = lazy

    @staticmethod
    def always(value: Any) -> AlwaysProp:
        return AlwaysProp(value)

    @staticmethod
    def defer(callback: Callable[[], Any], group: str = "default", *, merge: bool = False) -> DeferProp:
        return DeferProp(callback, group=group, merge=merge)

    @staticmethod
    def merge(value: Any, *, deep: bool = False, match_on: list[str] | None = None) -> MergeProp:
        return MergeProp(value, deep=deep, match_on=match_on)

    @staticmethod
    def deep_merge(value: Any, *, match_on: list[str] | None = None) -> MergeProp:
        return MergeProp(value, deep=True, match_on=match_on)


# --------------------------------------------------------------------------- #
# ASGI middleware (version 409, Vary, 303 redirects, shared data, state)
# --------------------------------------------------------------------------- #
class InertiaMiddleware:
    def __init__(self, app: Any, *, config: dict[str, Any] | InertiaConfig | None = None) -> None:
        self.app = app
        self.config = config if isinstance(config, InertiaConfig) else InertiaConfig(**(config or {}))

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        is_inertia = request.headers.get("x-inertia") is not None

        state = _InertiaState(request=request)
        # seed shared data from config
        share = self.config.share
        if callable(share):
            state.shared.update(share(request) or {})
        elif isinstance(share, dict):
            state.shared.update(share)
        # consume session flash data (validation errors from the previous
        # request become the auto-shared `errors` prop, Laravel-style)
        _consume_flash(state)
        token_reset = _current.set(state)

        # asset version check: stale GET -> force a full reload (409 + Location)
        if is_inertia and request.method == "GET":
            client_version = request.headers.get("x-inertia-version", "")
            current_version = _resolve_version(self.config, state)
            if current_version and client_version != current_version:
                # reflash so flashed data (e.g. errors) survives the forced
                # full reload, matching Laravel's session reflash on 409
                session = scope.get("session")
                if state.flash_consumed and state.flash and isinstance(session, dict):
                    session[_FLASH_KEY] = state.flash
                _current.reset(token_reset)
                response = Response(
                    status_code=409,
                    headers={"X-Inertia-Location": str(request.url)},
                )
                await response(scope, receive, send)
                return

        pending_start: dict[str, Any] | None = None

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal pending_start
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # always advertise Vary: X-Inertia
                if not any(k.lower() == b"vary" for k, _ in headers):
                    headers.append((b"vary", b"X-Inertia"))
                # redirect after PUT/PATCH/DELETE must be 303 so the browser
                # re-issues the follow-up as GET. Only 302 is converted —
                # Laravel leaves explicit 301/307/308 choices untouched.
                if (
                    is_inertia
                    and request.method in {"PUT", "PATCH", "DELETE"}
                    and message.get("status") == 302
                ):
                    message["status"] = 303
                message["headers"] = headers
                # An Inertia request that produced a bare 200 with no content
                # is redirected back (Laravel's onEmptyResponse); hold the
                # start message until the body reveals whether it is empty.
                if is_inertia and message.get("status") == 200:
                    pending_start = message
                    return
            elif message["type"] == "http.response.body" and pending_start is not None:
                start, pending_start = pending_start, None
                body = message.get("body", b"")
                if not message.get("more_body") and body in (b"", b"null"):
                    referer = request.headers.get("referer") or "/"
                    status = 303 if request.method in {"PUT", "PATCH", "DELETE"} else 302
                    await send(
                        {
                            "type": "http.response.start",
                            "status": status,
                            "headers": [
                                (b"location", referer.encode("latin-1")),
                                (b"vary", b"X-Inertia"),
                            ],
                        }
                    )
                    await send({"type": "http.response.body", "body": b""})
                    return
                await send(start)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _current.reset(token_reset)


# --------------------------------------------------------------------------- #
# Module
# --------------------------------------------------------------------------- #
class InertiaModule:
    @staticmethod
    def for_root(
        *,
        root_view: str = "app",
        root_element: str = "app",
        version: str | Callable[[], str] | None = None,
        template: str | Callable[..., str] | None = None,
        share: Callable[[Request], dict[str, Any]] | dict[str, Any] | None = None,
        encrypt_history: bool = False,
        ssr: dict[str, Any] | bool | None = None,
        vite: dict[str, Any] | None = None,
        is_global: bool = True,
    ) -> type:
        options = {
            "root_view": root_view,
            "root_element": root_element,
            "version": version,
            "template": template,
            "share": share,
            "encrypt_history": encrypt_history,
            "ssr": ssr,
            "vite": vite,
        }

        @Module(
            providers=[use_value(INERTIA_OPTIONS, options), InertiaService],
            exports=[InertiaService],
            global_module=is_global,
        )
        class DynamicInertiaModule:
            pass

        setattr(
            DynamicInertiaModule,
            "__fanest_app_middlewares__",
            [{"class": InertiaMiddleware, "options": {"config": InertiaConfig(**options)}}],
        )
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
