import base64
import hashlib
import hmac
import os
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


class UnsupportedSecurityFeatureError(NotImplementedError):
    pass


class CsrfModule:
    @staticmethod
    def for_root(**_: Any) -> type:
        raise UnsupportedSecurityFeatureError(
            "CSRF protection is not built into FaNest yet. "
            "Install a Starlette/FastAPI CSRF middleware explicitly and register it with @Module middleware."
        )


class PasswordHasher:
    def __init__(
        self,
        *,
        algorithm: str = "sha256",
        iterations: int = 600_000,
        salt_bytes: int = 16,
    ) -> None:
        if algorithm not in hashlib.algorithms_available:
            raise ValueError(f"Unsupported password hash algorithm: {algorithm}")
        if iterations < 100_000:
            raise ValueError("PasswordHasher iterations must be at least 100000")
        if salt_bytes < 16:
            raise ValueError("PasswordHasher salt_bytes must be at least 16")
        self.algorithm = algorithm
        self.iterations = iterations
        self.salt_bytes = salt_bytes

    def hash(self, password: str | bytes) -> str:
        password_bytes = _password_bytes(password)
        salt = os.urandom(self.salt_bytes)
        digest = hashlib.pbkdf2_hmac(self.algorithm, password_bytes, salt, self.iterations)
        return "pbkdf2_{algorithm}${iterations}${salt}${digest}".format(
            algorithm=self.algorithm,
            iterations=self.iterations,
            salt=base64.urlsafe_b64encode(salt).decode().rstrip("="),
            digest=base64.urlsafe_b64encode(digest).decode().rstrip("="),
        )

    def verify(self, password: str | bytes, encoded_hash: str) -> bool:
        try:
            scheme, iterations, salt, expected = encoded_hash.split("$", 3)
            prefix, algorithm = scheme.split("_", 1)
            if prefix != "pbkdf2":
                return False
            iteration_count = int(iterations)
            salt_bytes = _urlsafe_b64decode(salt)
            expected_digest = _urlsafe_b64decode(expected)
        except (ValueError, TypeError):
            return False
        try:
            digest = hashlib.pbkdf2_hmac(
                algorithm,
                _password_bytes(password),
                salt_bytes,
                iteration_count,
            )
        except ValueError:
            return False
        return hmac.compare_digest(digest, expected_digest)


def _password_bytes(password: str | bytes) -> bytes:
    if isinstance(password, bytes):
        return password
    return password.encode("utf-8")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


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
