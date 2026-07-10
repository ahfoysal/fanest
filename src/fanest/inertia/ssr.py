from __future__ import annotations

from typing import Any


# --------------------------------------------------------------------------- #
# SSR client (POST the page object to the Node render server)
# --------------------------------------------------------------------------- #
class InertiaSSR:
    def __init__(self, options: dict[str, Any] | bool | None) -> None:
        if options in (None, False):
            self.enabled = False
            self.url = ""
            self.throw_on_error = False
            return
        if options is True:
            options = {}
        self.enabled = bool(options.get("enabled", True))
        self.url = str(options.get("url", "http://127.0.0.1:13714")).rstrip("/")
        # Surface SSR failures (raise) instead of silently falling back to CSR.
        self.throw_on_error = bool(options.get("throw_on_error", False))

    async def render(self, page: dict[str, Any]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(f"{self.url}/render", json=page)
                response.raise_for_status()
                return response.json()
        except Exception:
            if self.throw_on_error:
                raise
            # Graceful fallback to client-side rendering if the SSR server is down.
            return None

    async def is_healthy(self) -> bool:
        """Ping the SSR server's ``/health`` endpoint (Laravel ``inertia:ssr`` health)."""
        if not self.enabled:
            return False
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.url}/health")
                return response.is_success
        except Exception:
            return False
