from __future__ import annotations

from typing import Any

from starlette.responses import Response

from fanest import BaseExceptionFilter, Catch
from fanest.inertia.context import _current
from fanest.inertia.service import InertiaService


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
    so non-error exceptions fall through to the normal handler. In ``debug`` mode
    the filter always re-raises so the developer sees the real traceback."""

    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    async def catch(self, exc: Exception, context: Any) -> Response | None:
        config = self.inertia.config
        # Local dev: surface the real error instead of a polished error page
        # (Laravel only renders Inertia error pages outside local/testing).
        if config.debug:
            return None
        # Defensive: without an active Inertia request there is no state to render
        # from, so fall through to the framework's default error handling.
        if _current.get() is None:
            return None
        status = getattr(exc, "status_code", None)
        if status is None:
            status = 500
        if int(status) not in config.error_statuses:
            return None  # not an error-page status -> re-raise the original exception
        response = ExceptionResponse(self.inertia, int(status), config.error_component, {"status": int(status)})
        return await response.render()
