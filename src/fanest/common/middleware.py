import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

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


class FaNestMiddlewareAdapter:
    def __init__(self, app: Any, *, middleware: Any, container: FaNestContainer) -> None:
        self.app = app
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

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        if not self._matches(request):
            await self.app(scope, receive, send)
            return
        instance = self.container.resolve(self.middleware) if inspect.isclass(self.middleware) else self.middleware
        if hasattr(instance, "use"):
            result = instance.use(request, self._call_next(scope, receive))
        else:
            result = instance(request, self._call_next(scope, receive))
        if inspect.isawaitable(result):
            result = await result
        await result(scope, receive, send)

    def _call_next(self, scope: dict[str, Any], receive: Callable[..., Any]) -> Callable[[Request], Any]:
        async def call_next(request: Request) -> Response:
            messages: list[dict[str, Any]] = []

            async def send_capture(message: dict[str, Any]) -> None:
                messages.append(message)

            request_receive = getattr(request, "_receive", receive)
            await self.app(scope, request_receive, send_capture)
            return self._response_from_messages(messages)

        return call_next

    def _response_from_messages(self, messages: list[dict[str, Any]]) -> Response:
        start = next((message for message in messages if message["type"] == "http.response.start"), None)
        body_messages = [message for message in messages if message["type"] == "http.response.body"]
        body = b"".join(message.get("body", b"") for message in body_messages)
        status_code = start.get("status", 200) if start else 200
        raw_headers = start.get("headers", []) if start else []
        response = Response(content=body, status_code=status_code)
        response.raw_headers = list(raw_headers)
        return response

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
            base = normalized[:-1]
            if base.endswith("/"):
                # '/x/*' segment wildcard: require the boundary slash so it
                # does not leak onto the bare parent or unrelated siblings.
                return path.startswith(base)
            # '/x*' prefix wildcard: match the base exactly or a sub-path,
            # but not siblings like '/x-admin'.
            return path.rstrip("/") == base or path.startswith(base + "/")
        return path.rstrip("/") == normalized.rstrip("/")
