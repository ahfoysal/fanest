import base64
import hashlib
import hmac
import json
from http.cookies import SimpleCookie
from typing import Any, Protocol
from uuid import uuid4

from fanest import Module


class SessionStore(Protocol):
    def load(self, session_id: str) -> dict[str, Any]: ...

    def save(self, session_id: str, session: dict[str, Any], *, max_age: int | None = None) -> None: ...

    def delete(self, session_id: str) -> None: ...


class MemorySessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}

    def load(self, session_id: str) -> dict[str, Any]:
        return dict(self.sessions.get(session_id, {}))

    def save(self, session_id: str, session: dict[str, Any], *, max_age: int | None = None) -> None:
        self.sessions[session_id] = dict(session)

    def delete(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


class RedisSessionStore:
    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379/0",
        prefix: str = "fanest:session:",
        client: Any | None = None,
    ) -> None:
        self.prefix = prefix
        if client is not None:
            self._client = client
            return
        try:
            import redis  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover - exercised without redis installed
            raise ImportError(
                "RedisSessionStore requires the 'redis' package. "
                "Install it with: pip install 'fanest[redis]'"
            ) from exc
        self._client = redis.Redis.from_url(url)

    def load(self, session_id: str) -> dict[str, Any]:
        raw = self._client.get(f"{self.prefix}{session_id}")
        if raw is None:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)

    def save(self, session_id: str, session: dict[str, Any], *, max_age: int | None = None) -> None:
        if max_age is not None and max_age <= 0:
            self.delete(session_id)
            return
        self._client.set(
            f"{self.prefix}{session_id}",
            json.dumps(session),
            ex=max_age,
        )

    def delete(self, session_id: str) -> None:
        self._client.delete(f"{self.prefix}{session_id}")

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


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
        store: SessionStore | None = None,
        rolling: bool = True,
        path: str = "/",
        domain: str | None = None,
    ) -> None:
        normalized_same_site = same_site.lower()
        if normalized_same_site not in {"lax", "strict", "none"}:
            raise ValueError("same_site must be one of: 'lax', 'strict', 'none'")
        if normalized_same_site == "none" and not https_only:
            raise ValueError("same_site='none' requires https_only=True")
        self.app = app
        self.secret_key = secret_key.encode()
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.https_only = https_only
        self.same_site = normalized_same_site
        self.store = store
        self.rolling = rolling
        self.path = path
        self.domain = domain

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session_id, session, had_cookie = self._load_session(scope)
        scope["session_id"] = session_id
        scope["session"] = session

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                cookie = self._cookie(
                    scope["session"],
                    session_id=scope.get("session_id"),
                    had_cookie=had_cookie,
                )
                if cookie is not None:
                    headers.append((b"set-cookie", cookie.encode()))
                message["headers"] = headers
            result = send(message)
            if hasattr(result, "__await__"):
                await result

        await self.app(scope, receive, send_with_cookie)

    def _load_session(self, scope) -> tuple[str | None, dict[str, Any], bool]:
        headers = dict(scope.get("headers", []))
        raw_cookie = headers.get(b"cookie")
        if raw_cookie is None:
            return ((str(uuid4()), {}, False) if self.store is not None else (None, {}, False))
        cookies = SimpleCookie(raw_cookie.decode())
        morsel = cookies.get(self.session_cookie)
        if morsel is None:
            return ((str(uuid4()), {}, False) if self.store is not None else (None, {}, False))
        try:
            payload, signature = morsel.value.rsplit(".", 1)
            if not hmac.compare_digest(signature, self._sign(payload)):
                return ((str(uuid4()), {}, True) if self.store is not None else (None, {}, True))
            if self.store is not None:
                return payload, self.store.load(payload), True
            decoded = base64.urlsafe_b64decode(payload.encode()).decode()
            return None, json.loads(decoded), True
        except Exception:
            return ((str(uuid4()), {}, True) if self.store is not None else (None, {}, True))

    def _cookie(self, session: dict[str, Any], *, session_id: str | None = None, had_cookie: bool = False) -> str | None:
        if not session and not had_cookie and not self.rolling:
            return None
        if self.store is not None:
            session_id = session_id or str(uuid4())
            if not session and had_cookie:
                self.store.delete(session_id)
                return self._expired_cookie()
            self.store.save(session_id, session, max_age=self.max_age)
            payload = session_id
        else:
            if not session and had_cookie:
                return self._expired_cookie()
            payload = base64.urlsafe_b64encode(json.dumps(session).encode()).decode()
        cookie = SimpleCookie()
        cookie[self.session_cookie] = f"{payload}.{self._sign(payload)}"
        cookie[self.session_cookie]["path"] = self.path
        cookie[self.session_cookie]["samesite"] = self.same_site
        cookie[self.session_cookie]["httponly"] = True
        if self.domain:
            cookie[self.session_cookie]["domain"] = self.domain
        if self.max_age is not None:
            cookie[self.session_cookie]["max-age"] = str(self.max_age)
        if self.https_only:
            cookie[self.session_cookie]["secure"] = True
        return cookie.output(header="").strip()

    def _expired_cookie(self) -> str:
        cookie = SimpleCookie()
        cookie[self.session_cookie] = ""
        cookie[self.session_cookie]["path"] = self.path
        cookie[self.session_cookie]["samesite"] = self.same_site
        cookie[self.session_cookie]["httponly"] = True
        cookie[self.session_cookie]["max-age"] = "0"
        if self.domain:
            cookie[self.session_cookie]["domain"] = self.domain
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
        store: SessionStore | None = None,
        redis_url: str | None = None,
        redis_prefix: str = "fanest:session:",
        redis_client: Any | None = None,
        rolling: bool = True,
        path: str = "/",
        domain: str | None = None,
    ) -> type:
        normalized_same_site = same_site.lower()
        if normalized_same_site not in {"lax", "strict", "none"}:
            raise ValueError("same_site must be one of: 'lax', 'strict', 'none'")
        if normalized_same_site == "none" and not https_only:
            raise ValueError("same_site='none' requires https_only=True")
        session_store = store
        if session_store is None and (redis_url is not None or redis_client is not None):
            session_store = RedisSessionStore(
                url=redis_url or "redis://localhost:6379/0",
                prefix=redis_prefix,
                client=redis_client,
            )

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
                        "same_site": normalized_same_site,
                        "store": session_store,
                        "rolling": rolling,
                        "path": path,
                        "domain": domain,
                    },
                }
            ],
        )
        return DynamicSessionModule
