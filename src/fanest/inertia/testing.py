"""Server-side Inertia testing assertions (Laravel ``AssertableInertia`` parity).

    from fanest.inertia.testing import assert_inertia

    response = client.get("/users")
    assert_inertia(response) \
        .component("Users/Index") \
        .has("users", 3) \
        .where("users.0.name", "Ada") \
        .missing("secret") \
        .url("/users") \
        .version("1")

Also exposes header helpers for partial reloads / deferred props so tests can
re-issue the follow-up requests the Inertia client would make.
"""

from __future__ import annotations

import html
import json
from typing import Any, Callable


def _extract_page(response: Any) -> dict[str, Any]:
    """Pull the Inertia page object out of a test response (JSON visit or the
    ``data-page`` attribute of an initial HTML document)."""
    headers = getattr(response, "headers", {})
    content_type = headers.get("content-type", "") if hasattr(headers, "get") else ""
    if "application/json" in content_type or (hasattr(headers, "get") and headers.get("x-inertia")):
        return response.json()
    text = response.text
    if 'data-page="' in text:
        encoded = text.split('data-page="', 1)[1].split('"', 1)[0]
        return json.loads(html.unescape(encoded))
    # script-tag variant — guard on the exact split marker so a partial match
    # raises the clear AssertionError below instead of an IndexError.
    marker = 'type="application/json">'
    if marker in text:
        raw = text.split(marker, 1)[1].split("</script>", 1)[0]
        return json.loads(raw)
    raise AssertionError("Response is not an Inertia response (no page object found)")


def _get_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        try:
            if isinstance(current, list):
                current = current[int(part)]
            elif isinstance(current, dict):
                current = current[part]
            else:
                raise KeyError(part)
        except (KeyError, IndexError, ValueError) as exc:
            raise AssertionError(f"Inertia prop path '{path}' not found") from exc
    return current


class AssertableInertia:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.page = _extract_page(response)
        self.props: dict[str, Any] = self.page.get("props", {})

    @classmethod
    def from_response(cls, response: Any) -> "AssertableInertia":
        return cls(response)

    def component(self, name: str) -> "AssertableInertia":
        actual = self.page.get("component")
        assert actual == name, f"Inertia component '{actual}' does not match expected '{name}'"
        return self

    def url(self, url: str) -> "AssertableInertia":
        actual = self.page.get("url")
        assert actual == url, f"Inertia url '{actual}' does not match expected '{url}'"
        return self

    def version(self, version: str) -> "AssertableInertia":
        actual = self.page.get("version")
        assert actual == version, f"Inertia version '{actual}' does not match expected '{version}'"
        return self

    def has(self, key: str, value: int | Callable[["AssertableInertia"], Any] | None = None) -> "AssertableInertia":
        found = _get_path(self.props, key)
        if isinstance(value, int) and not isinstance(value, bool):
            length = len(found)
            assert length == value, f"Inertia prop '{key}' has {length} item(s), expected {value}"
        elif callable(value):
            scoped = AssertableInertia.__new__(AssertableInertia)
            scoped.response = self.response
            scoped.page = self.page
            scoped.props = found[0] if isinstance(found, list) else found
            value(scoped)
        return self

    def has_all(self, keys: list[str]) -> "AssertableInertia":
        for key in keys:
            self.has(key)
        return self

    def where(self, key: str, value: Any) -> "AssertableInertia":
        actual = _get_path(self.props, key)
        assert actual == value, f"Inertia prop '{key}' is {actual!r}, expected {value!r}"
        return self

    def missing(self, key: str) -> "AssertableInertia":
        try:
            _get_path(self.props, key)
        except AssertionError:
            return self
        raise AssertionError(f"Inertia prop '{key}' is present but was expected to be missing")

    def count(self, key: str, length: int) -> "AssertableInertia":
        return self.has(key, length)

    def etc(self) -> "AssertableInertia":  # Laravel no-op marker
        return self


def assert_inertia(response: Any, callback: Callable[[AssertableInertia], Any] | None = None) -> AssertableInertia:
    page = AssertableInertia(response)
    if callback is not None:
        callback(page)
    return page


# ------------------------------------------------------------------ header helpers
def partial_headers(component: str, only: list[str] | None = None, except_: list[str] | None = None, version: str = "") -> dict[str, str]:
    """Build the headers the Inertia client sends for a partial reload."""
    headers = {"X-Inertia": "true", "X-Inertia-Partial-Component": component}
    if version:
        headers["X-Inertia-Version"] = version
    if only:
        headers["X-Inertia-Partial-Data"] = ",".join(only)
    if except_:
        headers["X-Inertia-Partial-Except"] = ",".join(except_)
    return headers


def deferred_headers(component: str, keys: list[str], version: str = "") -> dict[str, str]:
    """Headers the client sends to load a deferred-prop group."""
    return partial_headers(component, only=keys, version=version)
