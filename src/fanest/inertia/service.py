from __future__ import annotations

from typing import Any, Callable

from starlette.responses import Response

from fanest import Inject, Injectable
from fanest.inertia.context import INERTIA_OPTIONS, InertiaConfig, _consume_flash, _current, _FLASH_KEY, _InertiaState
from fanest.inertia.props import AlwaysProp, DeferProp, LazyProp, MergeProp, OnceProp
from fanest.inertia.rendering import _render_response, _with_fragment
from fanest.inertia.ssr import InertiaSSR


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
