from typing import Any, Callable

import httpx

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

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
    def register(is_global: bool = False, **options: Any) -> type:
        @Module(
            providers=[use_value(HTTP_OPTIONS, options), HttpService],
            exports=[HttpService],
            global_module=is_global,
        )
        class DynamicHttpModule:
            pass

        return DynamicHttpModule

    @staticmethod
    def register_async(
        *,
        use_factory: Callable[..., dict[str, Any]],
        inject: list[Any] | None = None,
        is_global: bool = False,
    ) -> type:
        @Module(
            providers=[provider_factory(HTTP_OPTIONS, use_factory, inject=inject or []), HttpService],
            exports=[HttpService],
            global_module=is_global,
        )
        class DynamicHttpModule:
            pass

        return DynamicHttpModule
