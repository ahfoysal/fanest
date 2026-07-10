from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, cast

from starlette.requests import Request


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


@dataclass
class ScrollProp:
    """Infinite-scroll prop (Inertia::scroll). The array under ``wrapper`` (default
    ``"data"``) is *merged* into the existing list — appended by default, or
    prepended when the client sends ``X-Inertia-Infinite-Scroll-Merge-Intent:
    prepend`` (scrolling up). Advertised under ``scrollProps`` with pagination
    metadata, and — unless the key is reset — under ``mergeProps`` (append) or
    ``prependProps`` (prepend) as ``"{key}.{wrapper}"``. ``metadata`` supplies the
    ``pageName``/``previousPage``/``nextPage``/``currentPage`` the client paginates
    with (a dict or a no-arg callable)."""

    value: Any
    wrapper: str = "data"
    metadata: dict[str, Any] | Callable[[], dict[str, Any]] | None = None
    match_on: list[str] | None = None


# --------------------------------------------------------------------------- #
# Prop resolution (partial reloads, lazy/always/defer/merge)
# --------------------------------------------------------------------------- #
async def _evaluate(value: Any, request: Request | None = None) -> Any:
    if isinstance(value, (LazyProp, DeferProp, OnceProp)):
        value = value.callback
    elif isinstance(value, AlwaysProp):
        value = value.value
    elif isinstance(value, (MergeProp, ScrollProp)):
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
    scroll: dict[str, dict[str, Any]] = field(default_factory=dict)


def _scroll_metadata(prop: "ScrollProp") -> dict[str, Any]:
    """The four pagination fields inertia-laravel emits per scroll prop."""
    meta = prop.metadata() if callable(prop.metadata) else prop.metadata
    meta = meta or {}
    return {
        "pageName": meta.get("pageName", "page"),
        "previousPage": meta.get("previousPage"),
        "nextPage": meta.get("nextPage"),
        "currentPage": meta.get("currentPage", 1),
    }


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
    # Infinite-scroll merge intent: "prepend" (scrolling up) else append.
    scroll_intent = request.headers.get("x-inertia-infinite-scroll-merge-intent")
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
        if isinstance(value, ScrollProp):
            # Advertised under scrollProps (always, with a `reset` flag) and — unless
            # reset — under mergeProps (append) / prependProps (prepend) as
            # "{key}.{wrapper}", per the infinite-scroll merge-intent header.
            merge_path = f"{key}.{value.wrapper}"
            is_reset = key in reset
            if not is_reset:
                if scroll_intent == "prepend":
                    out.prepend_keys.append(merge_path)
                else:
                    out.merge_keys.append(merge_path)
                for field_name in value.match_on or []:
                    out.match_on.append(f"{key}.{value.wrapper}.{field_name}")
            out.scroll[key] = {**_scroll_metadata(value), "reset": is_reset}
            resolved[key] = await _evaluate(value, request)
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
