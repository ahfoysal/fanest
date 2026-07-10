from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

from starlette.requests import Request

from fanest.core.providers import token

INERTIA_OPTIONS = token("INERTIA_OPTIONS")

#: Session key holding data flashed for exactly one follow-up request.
_FLASH_KEY = "_inertia_flash"


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
    # Defaults cover the common Laravel (resources/js/Pages) and Vite (src/pages,
    # src/Pages, resources/js/pages) layouts so the guard works without extra config.
    ensure_pages_exist: bool = False
    page_paths: list[str] = field(
        default_factory=lambda: [
            "resources/js/Pages",
            "resources/js/pages",
            "src/Pages",
            "src/pages",
        ]
    )
    page_extensions: list[str] = field(
        default_factory=lambda: ["js", "jsx", "svelte", "ts", "tsx", "vue"]
    )
    # Component + statuses used by InertiaExceptionFilter for error pages.
    error_component: str = "Error"
    error_statuses: tuple[int, ...] = (403, 404, 500, 503)
    # When True (local dev), InertiaExceptionFilter re-raises instead of rendering
    # the error page, so the developer sees the real traceback — mirrors Laravel
    # only rendering Inertia error pages outside the local/testing environments.
    debug: bool = False
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
