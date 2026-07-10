from __future__ import annotations

import html
import inspect
import json
from pathlib import Path
from typing import Any, Callable, cast

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from fanest.inertia.context import InertiaConfig, _consume_flash, _InertiaState
from fanest.inertia.props import _normalize_errors, _resolve_props
from fanest.inertia.ssr import InertiaSSR
from fanest.inertia.vite import ViteAssets


class InertiaComponentNotFoundError(RuntimeError):
    """Raised (when ``ensure_pages_exist`` is on) if a rendered component has no
    matching file under any configured page path — mirrors Laravel's guard."""


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
