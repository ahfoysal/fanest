import inspect
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from fanest.core.container import FaNestContainer


class FaNestMiddlewareAdapter(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, middleware: Any, container: FaNestContainer) -> None:
        super().__init__(app)
        self.middleware = middleware
        self.container = container

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Any:
        instance = self.container.resolve(self.middleware) if inspect.isclass(self.middleware) else self.middleware
        if hasattr(instance, "use"):
            result = instance.use(request, call_next)
        else:
            result = instance(request, call_next)
        if inspect.isawaitable(result):
            return await result
        return result
