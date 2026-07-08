from collections.abc import Callable
from typing import Any

from starlette.requests import Request

from fanest import Module


class SecurityHeadersMiddleware:
    def __init__(self, app: Any, *, headers: dict[str, str] | None = None) -> None:
        self.app = app
        self.headers = headers or {
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "no-referrer",
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {key.lower() for key, _ in headers}
                for key, value in self.headers.items():
                    encoded_key = key.lower().encode()
                    if encoded_key not in existing:
                        headers.append((encoded_key, value.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)

    async def use(self, request: Request, call_next: Callable[..., Any]) -> Any:
        response = await call_next(request)
        for key, value in self.headers.items():
            response.headers.setdefault(key, value)
        return response


class HelmetModule:
    @staticmethod
    def for_root(*, headers: dict[str, str] | None = None) -> type:
        @Module()
        class DynamicHelmetModule:
            pass

        setattr(
            DynamicHelmetModule,
            "__fanest_app_middlewares__",
            [{"class": SecurityHeadersMiddleware, "options": {"headers": headers}}],
        )
        return DynamicHelmetModule
