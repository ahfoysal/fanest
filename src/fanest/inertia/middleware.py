from __future__ import annotations

import copy
import re
import secrets
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import Response

from fanest.inertia.context import _consume_flash, _current, _FLASH_KEY, _InertiaState, InertiaConfig
from fanest.inertia.rendering import _resolve_version


# --------------------------------------------------------------------------- #
# ASGI middleware (version 409, Vary, 303 redirects, shared data, state)
# --------------------------------------------------------------------------- #
class InertiaMiddleware:
    def __init__(self, app: Any, *, config: dict[str, Any] | InertiaConfig | None = None) -> None:
        self.app = app
        self.config = config if isinstance(config, InertiaConfig) else InertiaConfig(**(config or {}))

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        is_inertia = request.headers.get("x-inertia") is not None

        state = _InertiaState(request=request)
        # seed shared data from config
        share = self.config.share
        if callable(share):
            result = share(request)
            if isinstance(result, dict):  # ignore a stray non-dict return
                state.shared.update(result)
        elif isinstance(share, dict):
            # deep-copy so a handler mutating a nested shared value can't leak it
            # into the process-wide config dict (and thus other requests).
            state.shared.update(copy.deepcopy(share))
        # consume session flash data (validation errors from the previous
        # request become the auto-shared `errors` prop, Laravel-style)
        _consume_flash(state)
        token_reset = _current.set(state)
        try:
            # asset version check: stale GET -> force a full reload (409 + Location)
            if is_inertia and request.method == "GET":
                client_version = request.headers.get("x-inertia-version", "")
                current_version = _resolve_version(self.config, state)
                if current_version and client_version != current_version:
                    # reflash so flashed data (e.g. errors) survives the forced
                    # full reload, matching Laravel's session reflash on 409
                    session = scope.get("session")
                    if state.flash_consumed and state.flash and isinstance(session, dict):
                        session[_FLASH_KEY] = state.flash
                    # percent-encode so a non-latin-1 URL (CJK/emoji slug) can't
                    # crash Starlette's latin-1 header encoding on the reload.
                    location = quote(str(request.url), safe=":/?#[]@!$&'()*+,;=~")
                    response = Response(status_code=409, headers={"X-Inertia-Location": location})
                    await response(scope, receive, send)
                    return

            pending_start: dict[str, Any] | None = None

            async def send_wrapper(message: dict[str, Any]) -> None:
                nonlocal pending_start
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    # always advertise Vary: X-Inertia — merge into any existing
                    # Vary rather than dropping ours (else a shared cache keyed
                    # without X-Inertia could serve a JSON page to a browser).
                    vary_idx = next((i for i, (k, _) in enumerate(headers) if k.lower() == b"vary"), None)
                    if vary_idx is None:
                        headers.append((b"vary", b"X-Inertia"))
                    elif b"x-inertia" not in headers[vary_idx][1].lower():
                        headers[vary_idx] = (headers[vary_idx][0], headers[vary_idx][1] + b", X-Inertia")
                    # redirect after PUT/PATCH/DELETE must be 303 so the browser
                    # re-issues the follow-up as GET. Only 302 is converted —
                    # Laravel leaves explicit 301/307/308 choices untouched.
                    if (
                        is_inertia
                        and request.method in {"PUT", "PATCH", "DELETE"}
                        and message.get("status") == 302
                    ):
                        message["status"] = 303
                    message["headers"] = headers
                    # An Inertia request that produced an empty OK response is
                    # redirected back (Laravel's onEmptyResponse); hold the start
                    # message until the body reveals whether it is empty. Both 200
                    # and 201 are treated as "OK" because a handler returning nothing
                    # defaults to 200 on GET and 201 on POST (NestJS semantics).
                    if is_inertia and message.get("status") in (200, 201):
                        pending_start = message
                        return
                elif message["type"] == "http.response.body" and pending_start is not None:
                    start, pending_start = pending_start, None
                    body = message.get("body", b"")
                    if not message.get("more_body") and body in (b"", b"null"):
                        referer = request.headers.get("referer") or "/"
                        status = 303 if request.method in {"PUT", "PATCH", "DELETE"} else 302
                        await send(
                            {
                                "type": "http.response.start",
                                "status": status,
                                "headers": [
                                    (b"location", referer.encode("latin-1")),
                                    (b"vary", b"X-Inertia"),
                                ],
                            }
                        )
                        await send({"type": "http.response.body", "body": b""})
                        return
                    await send(start)
                await send(message)

            await self.app(scope, receive, send_wrapper)
        finally:
            _current.reset(token_reset)


# --------------------------------------------------------------------------- #
# Method spoofing (Laravel _method) — file-upload PUT/PATCH/DELETE over POST
# --------------------------------------------------------------------------- #
def _extract_method_field(body: bytes, content_type: str) -> str | None:
    text = body.decode("latin-1", "ignore")
    if "x-www-form-urlencoded" in content_type:
        from urllib.parse import parse_qs

        values = parse_qs(text)
        method = values.get("_method")
        return method[0].upper() if method else None
    if "form-data" in content_type:
        match = re.search(r'name="_method"\r?\n\r?\n([A-Za-z]+)', text)
        return match.group(1).upper() if match else None
    return None


class MethodOverrideMiddleware:
    """Re-dispatch a POST as PUT/PATCH/DELETE when it carries a ``_method`` form
    field (or ``X-HTTP-Method-Override`` header) — so Inertia file-upload forms
    can hit update/delete handlers (browsers can't send those verbs multipart)."""

    _SPOOFABLE = {"PUT", "PATCH", "DELETE"}
    _MAX_SCAN = 64 * 1024  # only scan the head of a form body for the `_method` field

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        override = headers.get(b"x-http-method-override")
        override_method = override.decode("latin-1").upper() if override else None
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        if override_method is None and ("form-data" in content_type or "x-www-form-urlencoded" in content_type):
            # The `_method` field is an early form field, so only buffer up to a
            # cap to locate it; a large upload then streams the rest straight to
            # the app instead of being held (and copied) whole in memory.
            body = bytearray()
            messages: list[dict[str, Any]] = []
            capped = False
            more = True
            while more:
                message = await receive()
                messages.append(message)
                if message.get("type") == "http.request":
                    body.extend(message.get("body", b""))
                    more = message.get("more_body", False)
                    if len(body) > self._MAX_SCAN:
                        capped = True
                        break
                else:
                    more = False
            override_method = _extract_method_field(bytes(body), content_type)
            iterator = iter(messages)
            original_receive = receive

            async def replay() -> dict[str, Any]:
                try:
                    return next(iterator)
                except StopIteration:
                    # oversized body: hand the remaining chunks straight from the client
                    if capped:
                        return await original_receive()
                    return {"type": "http.request", "body": b"", "more_body": False}

            receive = replay
        if override_method in self._SPOOFABLE:
            scope = {**scope, "method": override_method}
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
# CSRF — double-submit XSRF-TOKEN cookie + X-XSRF-TOKEN header (axios default)
# --------------------------------------------------------------------------- #
def _tokens_match(cookie_token: str | None, header_token: str | None) -> bool:
    """Constant-time double-submit comparison. A missing or non-ASCII token is a
    clean mismatch (419), never a 500 from ``compare_digest`` raising on unicode."""
    if not cookie_token or not header_token:
        return False
    try:
        return secrets.compare_digest(header_token, cookie_token)
    except TypeError:
        return False


class InertiaCsrfMiddleware:
    """Issues an ``XSRF-TOKEN`` cookie and verifies the matching ``X-XSRF-TOKEN``
    header on state-changing requests — the CSRF story the Inertia/axios client
    expects. Uses the stateless double-submit-cookie pattern."""

    _PROTECTED = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(
        self,
        app: Any,
        *,
        cookie_name: str = "XSRF-TOKEN",
        header_name: str = "X-XSRF-TOKEN",
        exclude: list[str] | None = None,
        same_site: str = "lax",
        secure: bool = False,
    ) -> None:
        self.app = app
        self.cookie_name = cookie_name
        self.header_name = header_name.lower()
        self.exclude = set(exclude or [])
        self.same_site = same_site
        self.secure = secure

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        cookie_token = request.cookies.get(self.cookie_name)
        if request.method in self._PROTECTED and request.url.path not in self.exclude:
            header_token = request.headers.get(self.header_name)
            if not _tokens_match(cookie_token, header_token):
                # 419 like Laravel; the Inertia client surfaces "page expired".
                response = Response("CSRF token mismatch", status_code=419, headers={"Vary": "X-Inertia"})
                await response(scope, receive, send)
                return
        issued = cookie_token or secrets.token_urlsafe(32)

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start" and cookie_token is None:
                headers = list(message.get("headers", []))
                cookie = f"{self.cookie_name}={issued}; Path=/; SameSite={self.same_site}"
                if self.secure:
                    cookie += "; Secure"
                headers.append((b"set-cookie", cookie.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# --------------------------------------------------------------------------- #
# Encrypt-history route middleware (force history encryption on select routes)
# --------------------------------------------------------------------------- #
class EncryptHistoryMiddleware:
    """Opt a subset of routes into history encryption — the equivalent of Laravel's
    ``EncryptHistory`` route middleware. A path matches when it equals a configured
    entry, or (for entries ending in ``*``) shares its prefix::

        EncryptHistoryMiddleware(app, paths=["/account", "/admin*"])

    NOTE: like Laravel's middleware, this does NOT perform any server-side
    encryption. It only sets ``encryptHistory: true`` on the page object; the
    Inertia client is what encrypts the serialized history state in the browser.

    Must run *inside* ``InertiaMiddleware`` (it mutates the per-request state), so
    ``for_root`` inserts it as the innermost middleware."""

    def __init__(self, app: Any, *, paths: list[str] | None = None) -> None:
        self.app = app
        self.exact: set[str] = set()
        self.prefixes: list[str] = []
        for entry in paths or []:
            if entry.endswith("*"):
                self.prefixes.append(entry[:-1])
            else:
                self.exact.add(entry)

    def _matches(self, path: str) -> bool:
        return path in self.exact or any(path.startswith(prefix) for prefix in self.prefixes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self._matches(scope.get("path", "")):
            state = _current.get()
            if state is not None:
                state.encrypt_history = True
        await self.app(scope, receive, send)
