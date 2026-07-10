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
import inspect
import json
import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from fanest import BaseExceptionFilter, Catch, Controller, Get, Inject, Injectable, Module, use_value
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
    advertised under ``deferredProps`` on the initial page object. ``rescue=True``
    swallows a callback error (returns ``None``) instead of breaking the fetch."""

    callback: Callable[[], Any]
    group: str = "default"
    merge: bool = False
    rescue: bool = False


@dataclass
class MergeProp:
    """Included normally, but the client merges (instead of replaces) it — its key
    is advertised under ``mergeProps`` (shallow append), ``prependProps`` (shallow
    prepend) or ``deepMergeProps`` (deep), for Inertia v2 infinite scroll. ``match_on``
    fields are advertised under ``matchPropsOn`` so the client matches array items by
    key instead of index."""

    value: Any
    deep: bool = False
    match_on: list[str] | None = None
    prepend: bool = False


@dataclass
class OnceProp:
    """Sent once, then retained client-side: the client caches it and sends
    ``X-Inertia-Except-Once-Props`` on later visits to skip re-fetching. Advertised
    under ``onceProps`` on the page object."""

    callback: Callable[[], Any]
    expires_at: int | None = None


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
    root_view: str | Callable[[Request], str] = "app"
    root_element: str = "app"
    # ``version=False`` disables asset versioning entirely (no manifest hash, no 409).
    version: str | bool | Callable[..., str | bool | None] | None = None
    template: str | Callable[..., str] | dict[str, str | Callable[..., str]] | None = None
    share: Callable[[Request], dict[str, Any]] | dict[str, Any] | None = None
    encrypt_history: bool | Callable[[], bool] = False
    ssr: dict[str, Any] | bool | None = None
    vite: dict[str, Any] | None = None
    transform_component: Callable[[str], str] | None = None
    resolve_url: Callable[[Request], str] | None = None
    # Share every validation message per field (list) instead of just the first (str).
    with_all_errors: bool = False
    # Optional render-time guard that the component file actually exists on disk.
    ensure_pages_exist: bool = False
    page_paths: list[str] = field(default_factory=lambda: ["resources/js/Pages"])
    page_extensions: list[str] = field(
        default_factory=lambda: ["js", "jsx", "svelte", "ts", "tsx", "vue"]
    )
    # Component + statuses used by InertiaExceptionFilter for error pages.
    error_component: str = "Error"
    error_statuses: tuple[int, ...] = (403, 404, 500, 503)
    # Carry a URL #fragment through back()/location()/redirect helpers.
    preserve_fragment: bool = True


class HandleInertiaRequests:
    """Subclassable, object-oriented alternative to ``for_root``'s flat callbacks
    — the equivalent of Laravel's ``App\\Http\\Middleware\\HandleInertiaRequests``.

    Override any of ``version`` / ``share`` / ``root_view`` / ``encrypt_history``
    in a subclass and pass it in::

        class AppInertia(HandleInertiaRequests):
            def share(self, request):
                return {"auth": {"user": getattr(request.state, "user", None)}}
            def version(self, request):
                return "1.0"

        InertiaModule.for_root(handler=AppInertia)

    Explicit ``for_root`` keyword arguments still win over the handler's methods.
    """

    def version(self, request: Request) -> str | bool | None:
        """Asset version. ``None`` -> hash the Vite manifest; ``False`` -> disabled."""
        return None

    def share(self, request: Request) -> dict[str, Any]:
        """Props shared with every response for this request."""
        return {}

    def root_view(self, request: Request) -> str:
        """The Blade/HTML root template that hosts the Inertia app."""
        return "app"

    def encrypt_history(self) -> bool:
        """Whether to encrypt the browser history state for this request."""
        return False


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
            self.throw_on_error = False
            return
        if options is True:
            options = {}
        self.enabled = bool(options.get("enabled", True))
        self.url = str(options.get("url", "http://127.0.0.1:13714")).rstrip("/")
        # Surface SSR failures (raise) instead of silently falling back to CSR.
        self.throw_on_error = bool(options.get("throw_on_error", False))

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
            if self.throw_on_error:
                raise
            # Graceful fallback to client-side rendering if the SSR server is down.
            return None

    async def is_healthy(self) -> bool:
        """Ping the SSR server's ``/health`` endpoint (Laravel ``inertia:ssr`` health)."""
        if not self.enabled:
            return False
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.url}/health")
                return response.is_success
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# Prop resolution (partial reloads, lazy/always/defer/merge)
# --------------------------------------------------------------------------- #
async def _evaluate(value: Any, request: Request | None = None) -> Any:
    if isinstance(value, (LazyProp, DeferProp, OnceProp)):
        value = value.callback
    elif isinstance(value, AlwaysProp):
        value = value.value
    elif isinstance(value, MergeProp):
        value = value.value
    if callable(value) and not isinstance(value, type):
        # Laravel closures receive the Request: pass it when the callable accepts an arg.
        try:
            takes_request = len(inspect.signature(value).parameters) >= 1
        except (TypeError, ValueError):
            takes_request = False
        value = value(request) if (takes_request and request is not None) else value()
    if hasattr(value, "__await__"):
        value = await cast(Awaitable[Any], value)
    # ProvidesInertiaProperty / Arrayable / Responsable auto-resolution (like Laravel).
    to_prop = getattr(value, "to_inertia_property", None)
    if callable(to_prop):
        return to_prop()
    to_array = getattr(value, "to_array", None) or getattr(value, "toArray", None)
    if callable(to_array):
        return to_array()
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


@dataclass
class _ResolvedProps:
    """Everything ``_render_response`` needs to build the page object: the
    evaluated props plus the v2 metadata lists (only emitted when non-empty)."""

    props: dict[str, Any]
    deferred: dict[str, list[str]] = field(default_factory=dict)
    merge_keys: list[str] = field(default_factory=list)
    deep_merge_keys: list[str] = field(default_factory=list)
    prepend_keys: list[str] = field(default_factory=list)
    match_on: list[str] = field(default_factory=list)
    once: dict[str, dict[str, Any]] = field(default_factory=dict)


async def _resolve_props(
    props: dict[str, Any],
    *,
    component: str,
    request: Request,
) -> _ResolvedProps:
    partial_component = request.headers.get("x-inertia-partial-component")
    is_partial = partial_component == component
    # An absent/empty Partial-Data header means "no only-filter" (Laravel only
    # applies the filter when the list is non-empty), so except-only partial
    # reloads still receive every non-excepted prop.
    only = (_split_header(request.headers.get("x-inertia-partial-data")) or None) if is_partial else None
    excepted = _split_header(request.headers.get("x-inertia-partial-except")) if is_partial else []
    reset = set(_split_header(request.headers.get("x-inertia-reset")))
    # Once props the client already cached — advertised back so we skip re-sending them.
    except_once = set(_split_header(request.headers.get("x-inertia-except-once-props")))
    only_top = {p.split(".", 1)[0] for p in only} if only is not None else None
    except_top = {p for p in excepted if "." not in p}

    out = _ResolvedProps(props={})
    resolved = out.props
    always_props: dict[str, Any] = {}

    for key, value in props.items():
        # `always` props (and the shared `errors` bag) appear on every response,
        # so they bypass only/except filtering and are re-merged at the end.
        if isinstance(value, AlwaysProp) or key == "errors":
            always_props[key] = await _evaluate(value, request)
            continue
        # Once props: skip entirely when the client says it already has them cached,
        # otherwise send once + register the key so the client caches it.
        if isinstance(value, OnceProp) and key in except_once:
            continue
        if is_partial:
            if only_top is not None and key not in only_top:
                continue
            if key in except_top:
                continue
        elif isinstance(value, IgnoreOnFirstLoad):
            if isinstance(value, DeferProp):
                out.deferred.setdefault(value.group, []).append(key)
            continue
        if key not in reset:  # X-Inertia-Reset -> client replaces instead of merges
            if isinstance(value, MergeProp):
                # Deep wins over prepend: inertia-laravel's prepend/append resolvers
                # both reject deep props, so a deep+prepend prop lands in deepMergeProps.
                if value.deep:
                    out.deep_merge_keys.append(key)
                elif value.prepend:
                    out.prepend_keys.append(key)
                else:
                    out.merge_keys.append(key)
                for field_name in value.match_on or []:
                    out.match_on.append(f"{key}.{field_name}")
            elif isinstance(value, DeferProp) and value.merge:
                out.merge_keys.append(key)
        if isinstance(value, OnceProp):
            out.once[key] = {"prop": key, "expiresAt": value.expires_at}
        # `rescue` deferred props swallow a callback error (Laravel Inertia::defer
        # rescue) so one broken group cannot break the whole partial reload.
        if isinstance(value, DeferProp) and value.rescue:
            try:
                resolved[key] = await _evaluate(value, request)
            except Exception:
                resolved[key] = None
        else:
            resolved[key] = await _evaluate(value, request)

    # dot-notation: build the `only` subset, then forget dotted `except` from it
    if only is not None and any("." in p for p in only):
        resolved = _apply_only_paths(resolved, only)
    dotted_except = [p for p in excepted if "." in p]
    if dotted_except:
        resolved = _forget_paths(resolved, dotted_except)

    resolved.update(always_props)
    out.props = resolved
    return out


def _split_header(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


class InertiaComponentNotFoundError(RuntimeError):
    """Raised (when ``ensure_pages_exist`` is on) if a rendered component has no
    matching file under any configured page path — mirrors Laravel's guard."""


def _normalize_errors(errors: Any, with_all: bool) -> Any:
    """Match inertia-laravel's ``resolveValidationErrors``: by default each field
    maps to its FIRST message (string); ``with_all=True`` keeps every message as a
    list. Nested error bags are normalized recursively."""
    if not isinstance(errors, dict):
        return errors
    normalized: dict[str, Any] = {}
    for field_name, messages in errors.items():
        if isinstance(messages, dict):
            normalized[field_name] = _normalize_errors(messages, with_all)
        elif isinstance(messages, (list, tuple)):
            if not messages:
                normalized[field_name] = [] if with_all else ""
            else:
                normalized[field_name] = list(messages) if with_all else messages[0]
        else:
            normalized[field_name] = [messages] if with_all else messages
    return normalized


def _ensure_component_exists(config: InertiaConfig, component: str) -> None:
    relative = component.replace("\\", "/")
    for base in config.page_paths:
        base_path = Path(base)
        for ext in config.page_extensions:
            if (base_path / f"{relative}.{ext.lstrip('.')}").exists():
                return
    searched = ", ".join(config.page_paths) or "(no page paths configured)"
    raise InertiaComponentNotFoundError(
        f"Inertia page component '{component}' was not found in any of: {searched} "
        f"(extensions: {', '.join(config.page_extensions)})."
    )


def _with_fragment(url: str, fragment: str | None, enabled: bool) -> str:
    """Carry a URL ``#fragment`` through a redirect (Laravel ``->withFragment()``).
    An explicit fragment replaces one already on the URL; existing fragments are
    otherwise left intact."""
    if not enabled or not fragment:
        return url
    base = url.split("#", 1)[0]
    return f"{base}#{fragment.lstrip('#')}"


# --------------------------------------------------------------------------- #
# Page building + response rendering
# --------------------------------------------------------------------------- #
def _resolve_version(config: InertiaConfig, state: _InertiaState) -> str:
    if state.version is not None:
        return state.version
    version = config.version
    # ``version=False`` disables versioning: empty string -> no manifest hash and
    # the middleware never issues a 409 (current_version is falsy).
    if version is False:
        return ""
    if callable(version):
        try:
            takes_request = len(inspect.signature(version).parameters) >= 1
        except (TypeError, ValueError):
            takes_request = False
        version = version(state.request) if takes_request else version()
    if version is None:
        # Laravel default: hash the Vite manifest so a rebuild busts the client cache.
        return ViteAssets(config.vite).version_hash()
    if version is False:
        return ""
    return str(version)


def _default_template(vite: ViteAssets, head: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>FaNest</title>\n"
        f"{vite.tags()}\n{head}\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>"
    )


def _resolve_url(config: InertiaConfig, request: Request) -> str:
    if config.resolve_url is not None:
        return config.resolve_url(request)
    return request.url.path + (("?" + request.url.query) if request.url.query else "")


def _render_template(
    config: InertiaConfig,
    root_view: str | None,
    vite: ViteAssets,
    head: str,
    body: str,
    view_data: dict[str, Any],
    page: dict[str, Any],
) -> str:
    default_view = config.root_view if isinstance(config.root_view, str) else "app"
    view = root_view or default_view
    template = config.template
    if isinstance(template, dict):  # named templates -> select by root view
        template = template.get(view, _default_template)
    if template is None:
        template = _default_template
    if callable(template):
        # Pass as many of (vite, head, body, view_data, page) as the callable accepts,
        # so old 3-arg templates keep working and richer ones get view data + the page.
        render_fn = cast(Callable[..., str], template)
        args: tuple[Any, ...] = (vite, head, body, view_data, page)
        try:
            count = len(inspect.signature(render_fn).parameters)
        except (TypeError, ValueError):
            count = 3
        return render_fn(*args[: max(3, min(count, 5))])
    return str(template).replace("@inertiaHead", f"{vite.tags()}\n{head}").replace("@inertia", body)


async def _render_response(
    config: InertiaConfig,
    state: _InertiaState,
    component: str,
    props: dict[str, Any],
    *,
    root_view: str | None = None,
    view_data: dict[str, Any] | None = None,
    cache: Any = None,
    encrypt: bool | None = None,
    disable_ssr: bool = False,
) -> Response:
    request = state.request
    _consume_flash(state)
    if config.transform_component is not None:
        component = config.transform_component(component)
    # shared data is merged under the page props; explicit props win on key clash
    merged = {**state.shared, **props}
    resolved_props = await _resolve_props(merged, component=component, request=request)
    resolved = resolved_props.props

    # `errors` is always present; normalize (first-message vs all) then nest it
    # under the error bag when one is requested.
    errors = _normalize_errors(resolved.get("errors", {}), config.with_all_errors)
    error_bag = request.headers.get("x-inertia-error-bag")
    if error_bag and isinstance(errors, dict) and errors and error_bag not in errors:
        errors = {error_bag: errors}
    resolved["errors"] = errors

    if config.ensure_pages_exist:
        _ensure_component_exists(config, component)

    # ``encrypt_history`` may be a bool or a no-arg callable (middleware-base override).
    config_encrypt = config.encrypt_history() if callable(config.encrypt_history) else config.encrypt_history
    page: dict[str, Any] = {
        "component": component,
        "props": resolved,
        "url": _resolve_url(config, request),
        "version": _resolve_version(config, state),
        # history booleans are always emitted (matches inertia-laravel)
        "clearHistory": state.clear_history,
        "encryptHistory": encrypt if encrypt is not None else (state.encrypt_history or config_encrypt),
    }
    # v2 metadata: emitted only when non-empty, in inertia-laravel's key order.
    if resolved_props.merge_keys:
        page["mergeProps"] = resolved_props.merge_keys
    if resolved_props.prepend_keys:
        page["prependProps"] = resolved_props.prepend_keys
    if resolved_props.deep_merge_keys:
        page["deepMergeProps"] = resolved_props.deep_merge_keys
    if resolved_props.match_on:
        page["matchPropsOn"] = resolved_props.match_on
    if resolved_props.deferred:
        page["deferredProps"] = resolved_props.deferred
    if resolved_props.once:
        page["onceProps"] = resolved_props.once
    if cache is not None:
        page["cache"] = list(cache) if isinstance(cache, (list, tuple)) else [cache]

    # X-Inertia visit -> JSON page object
    if request.headers.get("x-inertia"):
        return JSONResponse(page, headers={"X-Inertia": "true", "Vary": "X-Inertia"})

    # First visit -> full HTML document (optionally server-side rendered)
    vite = ViteAssets(config.vite)
    ssr_result = None
    ssr = InertiaSSR(config.ssr)
    if ssr.enabled and not disable_ssr:
        ssr_result = await ssr.render(page)

    if ssr_result is not None:
        head_fragments = ssr_result.get("head", [])
        head = "\n".join(head_fragments) if isinstance(head_fragments, list) else str(head_fragments)
        body = ssr_result.get("body", "")
    else:
        encoded = html.escape(json.dumps(page, separators=(",", ":"), default=str), quote=True)
        head = ""
        body = f'<div id="{html.escape(config.root_element, quote=True)}" data-page="{encoded}"></div>'

    # root view may come from the builder, a plain config string, or a
    # request-aware callable (middleware-base ``root_view(request)`` override).
    effective_root_view = root_view
    if effective_root_view is None and callable(config.root_view):
        effective_root_view = config.root_view(request)
    document = _render_template(config, effective_root_view, vite, head, body, view_data or {}, page)
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


class InertiaResponseBuilder:
    """Lazy, chainable Inertia response (Laravel's ``InertiaResponse``).

    Awaitable — ``await inertia.render("Users", {...}).with_("user", u).root_view("admin")``
    materializes the Starlette response at ``await`` time.
    """

    def __init__(self, config: InertiaConfig, state: _InertiaState, component: str, props: dict[str, Any]) -> None:
        self._config = config
        self._state = state
        self._component = component
        self._props = dict(props)
        self._root_view: str | None = None
        self._view_data: dict[str, Any] = {}
        self._cache: Any = None
        self._encrypt: bool | None = None
        self._disable_ssr = False

    def with_(self, key: str | dict[str, Any], value: Any = None) -> "InertiaResponseBuilder":
        """Add props to the response (Laravel ``->with()``)."""
        if isinstance(key, dict):
            self._props.update(key)
        else:
            self._props[key] = value
        return self

    with_props = with_

    def root_view(self, view: str) -> "InertiaResponseBuilder":
        self._root_view = view
        return self

    def with_view_data(self, data: dict[str, Any] | None = None, **kwargs: Any) -> "InertiaResponseBuilder":
        """Data for the root template only — NOT serialized into page props."""
        if data:
            self._view_data.update(data)
        self._view_data.update(kwargs)
        return self

    def cache(self, seconds: Any) -> "InertiaResponseBuilder":
        self._cache = seconds
        return self

    def encrypt_history(self, encrypt: bool = True) -> "InertiaResponseBuilder":
        self._encrypt = encrypt
        return self

    def disable_ssr(self) -> "InertiaResponseBuilder":
        self._disable_ssr = True
        return self

    async def _render(self) -> Response:
        return await _render_response(
            self._config,
            self._state,
            self._component,
            self._props,
            root_view=self._root_view,
            view_data=self._view_data,
            cache=self._cache,
            encrypt=self._encrypt,
            disable_ssr=self._disable_ssr,
        )

    def __await__(self):
        return self._render().__await__()


@Injectable()
class InertiaService:
    def __init__(self, options: dict[str, Any] = Inject(INERTIA_OPTIONS)):
        self.config = InertiaConfig(**(options or {}))

    def render(self, component: str, props: dict[str, Any] | None = None) -> InertiaResponseBuilder:
        return InertiaResponseBuilder(self.config, _state(), component, props or {})

    def flush_shared(self, key: str | None = None) -> "InertiaService":
        shared = _state().shared
        if key is None:
            shared.clear()
        else:
            shared.pop(key, None)
        return self

    def set_root_view(self, view: str) -> "InertiaService":
        self.config.root_view = view
        return self

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

    def location(self, url: str, *, fragment: str | None = None) -> Response:
        url = _with_fragment(url, fragment, self.config.preserve_fragment)
        state = _current.get()
        if state is not None and state.request.headers.get("x-inertia"):
            return Response(status_code=409, headers={"X-Inertia-Location": url})
        return Response(status_code=302, headers={"Location": url})

    def back(self, fallback: str = "/", status: int | None = None, *, fragment: str | None = None) -> Response:
        """Redirect to the Referer (Laravel ``back()``). PUT/PATCH/DELETE get a
        303 so the browser re-issues the follow-up visit as GET. ``fragment``
        carries a ``#anchor`` through the redirect (Laravel ``->withFragment()``)."""
        request = _state().request
        url = request.headers.get("referer") or fallback
        url = _with_fragment(url, fragment, self.config.preserve_fragment)
        if status is None:
            status = 303 if request.method in {"PUT", "PATCH", "DELETE"} else 302
        return Response(status_code=status, headers={"Location": url, "Vary": "X-Inertia"})

    def redirect(self, url: str, status: int = 302, *, fragment: str | None = None) -> Response:
        """Redirect to ``url`` (Laravel ``redirect()``), carrying a ``#fragment``."""
        url = _with_fragment(url, fragment, self.config.preserve_fragment)
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

    async def ssr_health(self) -> bool:
        """True if the configured SSR server responds on its ``/health`` endpoint."""
        return await InertiaSSR(self.config.ssr).is_healthy()

    # prop factories (Inertia::lazy / optional / always / defer / merge / once)
    @staticmethod
    def lazy(callback: Callable[[], Any]) -> LazyProp:
        return LazyProp(callback)

    optional = lazy

    @staticmethod
    def always(value: Any) -> AlwaysProp:
        return AlwaysProp(value)

    @staticmethod
    def defer(
        callback: Callable[[], Any],
        group: str = "default",
        *,
        merge: bool = False,
        rescue: bool = False,
    ) -> DeferProp:
        return DeferProp(callback, group=group, merge=merge, rescue=rescue)

    @staticmethod
    def merge(
        value: Any,
        *,
        deep: bool = False,
        match_on: list[str] | None = None,
        prepend: bool = False,
    ) -> MergeProp:
        return MergeProp(value, deep=deep, match_on=match_on, prepend=prepend)

    @staticmethod
    def deep_merge(value: Any, *, match_on: list[str] | None = None) -> MergeProp:
        return MergeProp(value, deep=True, match_on=match_on)

    @staticmethod
    def once(callback: Callable[[], Any], expires_at: int | None = None) -> OnceProp:
        """A prop sent once, then cached client-side (Inertia::once)."""
        return OnceProp(callback, expires_at=expires_at)

    def share_once(
        self, key: str, callback: Callable[[], Any], expires_at: int | None = None
    ) -> "InertiaService":
        """Share a once-prop across responses (Inertia::shareOnce)."""
        _state().shared[key] = OnceProp(callback, expires_at=expires_at)
        return self


# --------------------------------------------------------------------------- #
# Exception -> Inertia error page (Laravel's withExceptions error-page pattern)
# --------------------------------------------------------------------------- #
class ExceptionResponse:
    """How an exception is turned into an Inertia error page: a status code plus
    an ``Inertia::render(component, {...})`` with shared data re-attached. Mirrors
    the ``->toResponse($request)->setStatusCode($status)`` chain in Laravel."""

    def __init__(
        self,
        inertia: "InertiaService",
        status_code: int,
        component: str,
        props: dict[str, Any] | None = None,
    ) -> None:
        self._inertia = inertia
        self._status = int(status_code)
        self._component = component
        self._props = dict(props or {})
        self._shared: dict[str, Any] = {}

    def status_code(self) -> int:
        return self._status

    def with_shared_data(self, data: dict[str, Any]) -> "ExceptionResponse":
        """Attach extra shared data on top of the request's shared props."""
        self._shared.update(data or {})
        return self

    async def render(self) -> Response:
        builder = self._inertia.render(self._component, dict(self._props))
        if self._shared:
            builder.with_(dict(self._shared))
        response = await builder
        response.status_code = self._status
        return response


@Catch(Exception)
class InertiaExceptionFilter(BaseExceptionFilter):
    """Renders an Inertia error component for configured HTTP statuses (default
    403/404/500/503), preserving the status code and re-attaching the request's
    shared data. Register via ``global_filters`` (or an ``APP_FILTER`` provider)::

        FaNestFactory.create(AppModule, global_filters=[InertiaExceptionFilter])

    Statuses outside the configured set are re-raised unchanged (returns ``None``),
    so non-error exceptions fall through to the normal handler."""

    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    async def catch(self, exc: Exception, context: Any) -> Response | None:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = 500
        config = self.inertia.config
        if int(status) not in config.error_statuses:
            return None  # not an error-page status -> re-raise the original exception
        response = ExceptionResponse(self.inertia, int(status), config.error_component, {"status": int(status)})
        return await response.render()


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
                # An Inertia request that produced an empty OK response is
                # redirected back (Laravel's onEmptyResponse); hold the start
                # message until the body reveals whether it is empty. Both 200
                # and 201 are treated as "OK" because a handler returning nothing
                # defaults to 200 on GET and 201 on POST (NestJS semantics).
                if is_inertia and message.get("status") in (200, 201):
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
# Method spoofing (Laravel _method) — file-upload PUT/PATCH/DELETE over POST
# --------------------------------------------------------------------------- #
def _extract_method_field(body: bytes, content_type: str) -> str | None:
    text = body.decode("latin-1", "ignore")
    if "x-www-form-urlencoded" in content_type:
        from urllib.parse import parse_qs

        values = parse_qs(text)
        method = values.get("_method")
        return method[0].upper() if method else None
    if "form-data" in content_type:
        match = re.search(r'name="_method"\r?\n\r?\n([A-Za-z]+)', text)
        return match.group(1).upper() if match else None
    return None


class MethodOverrideMiddleware:
    """Re-dispatch a POST as PUT/PATCH/DELETE when it carries a ``_method`` form
    field (or ``X-HTTP-Method-Override`` header) — so Inertia file-upload forms
    can hit update/delete handlers (browsers can't send those verbs multipart)."""

    _SPOOFABLE = {"PUT", "PATCH", "DELETE"}

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        override = headers.get(b"x-http-method-override")
        override_method = override.decode("latin-1").upper() if override else None
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        if override_method is None and ("form-data" in content_type or "x-www-form-urlencoded" in content_type):
            body = bytearray()
            messages: list[dict[str, Any]] = []
            more = True
            while more:
                message = await receive()
                messages.append(message)
                if message.get("type") == "http.request":
                    body.extend(message.get("body", b""))
                    more = message.get("more_body", False)
                else:
                    more = False
            override_method = _extract_method_field(bytes(body), content_type)
            iterator = iter(messages)

            async def replay() -> dict[str, Any]:
                try:
                    return next(iterator)
                except StopIteration:
                    return {"type": "http.request", "body": b"", "more_body": False}

            receive = replay
        if override_method in self._SPOOFABLE:
            scope = {**scope, "method": override_method}
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
# CSRF — double-submit XSRF-TOKEN cookie + X-XSRF-TOKEN header (axios default)
# --------------------------------------------------------------------------- #
class InertiaCsrfMiddleware:
    """Issues an ``XSRF-TOKEN`` cookie and verifies the matching ``X-XSRF-TOKEN``
    header on state-changing requests — the CSRF story the Inertia/axios client
    expects. Uses the stateless double-submit-cookie pattern."""

    _PROTECTED = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(
        self,
        app: Any,
        *,
        cookie_name: str = "XSRF-TOKEN",
        header_name: str = "X-XSRF-TOKEN",
        exclude: list[str] | None = None,
        same_site: str = "lax",
        secure: bool = False,
    ) -> None:
        self.app = app
        self.cookie_name = cookie_name
        self.header_name = header_name.lower()
        self.exclude = set(exclude or [])
        self.same_site = same_site
        self.secure = secure

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        cookie_token = request.cookies.get(self.cookie_name)
        if request.method in self._PROTECTED and request.url.path not in self.exclude:
            header_token = request.headers.get(self.header_name)
            if not cookie_token or not header_token or not secrets.compare_digest(header_token, cookie_token):
                # 419 like Laravel; the Inertia client surfaces "page expired".
                response = Response("CSRF token mismatch", status_code=419, headers={"Vary": "X-Inertia"})
                await response(scope, receive, send)
                return
        issued = cookie_token or secrets.token_urlsafe(32)

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start" and cookie_token is None:
                headers = list(message.get("headers", []))
                cookie = f"{self.cookie_name}={issued}; Path=/; SameSite={self.same_site}"
                if self.secure:
                    cookie += "; Secure"
                headers.append((b"set-cookie", cookie.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --------------------------------------------------------------------------- #
# Encrypt-history route middleware (force history encryption on select routes)
# --------------------------------------------------------------------------- #
class EncryptHistoryMiddleware:
    """Force history encryption for a subset of routes — the equivalent of
    Laravel's ``EncryptHistory`` route middleware. A path matches when it equals
    a configured entry, or (for entries ending in ``*``) shares its prefix::

        EncryptHistoryMiddleware(app, paths=["/account", "/admin*"])

    Must run *inside* ``InertiaMiddleware`` (it mutates the per-request state), so
    ``for_root`` inserts it as the innermost middleware."""

    def __init__(self, app: Any, *, paths: list[str] | None = None) -> None:
        self.app = app
        self.exact: set[str] = set()
        self.prefixes: list[str] = []
        for entry in paths or []:
            if entry.endswith("*"):
                self.prefixes.append(entry[:-1])
            else:
                self.exact.add(entry)

    def _matches(self, path: str) -> bool:
        return path in self.exact or any(path.startswith(prefix) for prefix in self.prefixes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self._matches(scope.get("path", "")):
            state = _current.get()
            if state is not None:
                state.encrypt_history = True
        await self.app(scope, receive, send)


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
