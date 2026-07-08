import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from fanest.core.container import FaNestContainer


@dataclass(frozen=True)
class MiddlewareRoute:
    middleware: Any
    routes: list[str] = field(default_factory=lambda: ["*"])
    excluded: list[str] = field(default_factory=list)
    methods: list[str] | None = None


class MiddlewareConsumer:
    def __init__(self) -> None:
        self.middlewares: list[MiddlewareRoute] = []
        self._pending: list[Any] = []
        self._excluded: list[str] = []

    def apply(self, *middlewares: Any) -> "MiddlewareConsumer":
        self._pending = list(middlewares)
        self._excluded = []
        return self

    def exclude(self, *routes: str) -> "MiddlewareConsumer":
        self._excluded.extend(routes)
        return self

    def for_routes(self, *routes: str, methods: list[str] | None = None) -> "MiddlewareConsumer":
        targets = list(routes or ["*"])
        for middleware in self._pending:
            self.middlewares.append(
                MiddlewareRoute(
                    middleware=middleware,
                    routes=targets,
                    excluded=list(self._excluded),
                    methods=[method.upper() for method in methods] if methods else None,
                )
            )
        self._pending = []
        self._excluded = []
        return self


class FaNestMiddlewareAdapter(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, middleware: Any, container: FaNestContainer) -> None:
        super().__init__(app)
        if isinstance(middleware, MiddlewareRoute):
            self.middleware = middleware.middleware
            self.routes = middleware.routes
            self.excluded = middleware.excluded
            self.methods = middleware.methods
        else:
            self.middleware = middleware
            self.routes = ["*"]
            self.excluded = []
            self.methods = None
        self.container = container

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Any:
        if not self._matches(request):
            return await call_next(request)
        instance = self.container.resolve(self.middleware) if inspect.isclass(self.middleware) else self.middleware
        if hasattr(instance, "use"):
            result = instance.use(request, call_next)
        else:
            result = instance(request, call_next)
        if inspect.isawaitable(result):
            return await result
        return result

    def _matches(self, request: Request) -> bool:
        if self.methods is not None and request.method.upper() not in self.methods:
            return False
        path = request.url.path
        if any(self._path_matches(pattern, path) for pattern in self.excluded):
            return False
        return any(self._path_matches(pattern, path) for pattern in self.routes)

    def _path_matches(self, pattern: str, path: str) -> bool:
        if pattern == "*":
            return True
        normalized = "/" + pattern.strip("/")
        if normalized.endswith("*"):
            return path.startswith(normalized[:-1].rstrip("/"))
        return path.rstrip("/") == normalized.rstrip("/")
