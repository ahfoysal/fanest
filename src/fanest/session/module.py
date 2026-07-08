import base64
import hashlib
import hmac
import json
from http.cookies import SimpleCookie
from typing import Any

from fanest import Module


class FaNestSessionMiddleware:
    def __init__(
        self,
        app: Any,
        *,
        secret_key: str,
        session_cookie: str = "session",
        max_age: int | None = 14 * 24 * 60 * 60,
        https_only: bool = False,
        same_site: str = "lax",
    ) -> None:
        self.app = app
        self.secret_key = secret_key.encode()
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.https_only = https_only
        self.same_site = same_site

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope["session"] = self._load_session(scope)

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"set-cookie", self._cookie(scope["session"]).encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    def _load_session(self, scope) -> dict[str, Any]:
        headers = dict(scope.get("headers", []))
        raw_cookie = headers.get(b"cookie")
        if raw_cookie is None:
            return {}
        cookies = SimpleCookie(raw_cookie.decode())
        morsel = cookies.get(self.session_cookie)
        if morsel is None:
            return {}
        try:
            payload, signature = morsel.value.rsplit(".", 1)
            if not hmac.compare_digest(signature, self._sign(payload)):
                return {}
            decoded = base64.urlsafe_b64decode(payload.encode()).decode()
            return json.loads(decoded)
        except Exception:
            return {}

    def _cookie(self, session: dict[str, Any]) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(session).encode()).decode()
        cookie = SimpleCookie()
        cookie[self.session_cookie] = f"{payload}.{self._sign(payload)}"
        cookie[self.session_cookie]["path"] = "/"
        cookie[self.session_cookie]["samesite"] = self.same_site
        cookie[self.session_cookie]["httponly"] = True
        if self.max_age is not None:
            cookie[self.session_cookie]["max-age"] = str(self.max_age)
        if self.https_only:
            cookie[self.session_cookie]["secure"] = True
        return cookie.output(header="").strip()

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret_key, payload.encode(), hashlib.sha256).hexdigest()


class SessionModule:
    @staticmethod
    def for_root(
        *,
        secret_key: str,
        session_cookie: str = "session",
        max_age: int | None = 14 * 24 * 60 * 60,
        https_only: bool = False,
        same_site: str = "lax",
    ) -> type:
        @Module()
        class DynamicSessionModule:
            pass

        setattr(
            DynamicSessionModule,
            "__fanest_app_middlewares__",
            [
                {
                    "class": FaNestSessionMiddleware,
                    "options": {
                        "secret_key": secret_key,
                        "session_cookie": session_cookie,
                        "max_age": max_age,
                        "https_only": https_only,
                        "same_site": same_site,
                    },
                }
            ],
        )
        return DynamicSessionModule
