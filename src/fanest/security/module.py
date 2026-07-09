from collections.abc import Callable
from typing import Any

from starlette.requests import Request

from fanest import Module


class SecurityHeadersMiddleware:
    DEFAULT_HEADERS = {
        "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
        "origin-agent-cluster": "?1",
        "x-content-type-options": "nosniff",
        "x-dns-prefetch-control": "off",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        "strict-transport-security": "max-age=15552000; includeSubDomains",
        "x-download-options": "noopen",
        "x-permitted-cross-domain-policies": "none",
        "x-xss-protection": "0",
    }

    def __init__(
        self,
        app: Any,
        *,
        headers: dict[str, str | None] | None = None,
        include_defaults: bool = True,
    ) -> None:
        self.app = app
        merged_headers = {**self.DEFAULT_HEADERS, **(headers or {})} if include_defaults else headers or {}
        self.headers = _validate_headers(merged_headers)

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
    def for_root(*, headers: dict[str, str | None] | None = None) -> type:
        options_headers = _validate_headers({**SecurityHeadersMiddleware.DEFAULT_HEADERS, **(headers or {})})

        @Module()
        class DynamicHelmetModule:
            pass

        setattr(
            DynamicHelmetModule,
            "__fanest_app_middlewares__",
            [
                {
                    "class": SecurityHeadersMiddleware,
                    "options": {"headers": options_headers, "include_defaults": False},
                }
            ],
        )
        return DynamicHelmetModule


def _validate_headers(headers: dict[str, str | None]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for key, value in headers.items():
        normalized = key.strip().lower()
        if "\r" in key or "\n" in key:
            raise ValueError("Security headers cannot contain newline characters")
        if not normalized:
            raise ValueError("Security header names cannot be empty")
        if value is None:
            validated.pop(normalized, None)
            continue
        string_value = str(value)
        if "\r" in string_value or "\n" in string_value:
            raise ValueError("Security headers cannot contain newline characters")
        validated[normalized] = string_value
    return validated
