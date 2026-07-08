from typing import Any

import httpx

from fanest import Injectable, Module, use_value, Inject
from fanest.core.providers import token

HTTP_OPTIONS = token("HTTP_OPTIONS")


@Injectable()
class HttpService:
    def __init__(self, options: dict[str, Any] = Inject(HTTP_OPTIONS)):
        self.options = options
        self.client = httpx.AsyncClient(**options)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def on_application_shutdown(self):
        await self.client.aclose()


class HttpModule:
    @staticmethod
    def register(**options: Any) -> type:
        @Module(providers=[use_value(HTTP_OPTIONS, options), HttpService], exports=[HttpService])
        class DynamicHttpModule:
            pass

        return DynamicHttpModule
