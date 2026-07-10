import asyncio
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, cast

import httpx

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

HTTP_OPTIONS = token("HTTP_OPTIONS")


class HttpRequestInterceptor(Protocol):
    def __call__(
        self,
        request: httpx.Request,
    ) -> httpx.Request | Awaitable[httpx.Request | None] | None:
        ...


class HttpResponseInterceptor(Protocol):
    def __call__(
        self,
        response: httpx.Response,
    ) -> httpx.Response | Awaitable[httpx.Response | None] | None:
        ...


class HttpErrorInterceptor(Protocol):
    def __call__(
        self,
        error: Exception,
        request: httpx.Request,
    ) -> httpx.Response | Awaitable[httpx.Response | None] | None:
        ...


@dataclass(frozen=True)
class HttpModuleOptions:
    base_url: str | httpx.URL | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float | httpx.Timeout | None = None
    retries: int = 0
    retry_status_codes: set[int] = field(default_factory=lambda: {408, 425, 429, 500, 502, 503, 504})
    retry_methods: set[str] = field(default_factory=lambda: {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
    retry_backoff: float = 0
    request_interceptors: list[HttpRequestInterceptor] = field(default_factory=list)
    response_interceptors: list[HttpResponseInterceptor] = field(default_factory=list)
    error_interceptors: list[HttpErrorInterceptor] = field(default_factory=list)
    client_options: dict[str, Any] = field(default_factory=dict)


@Injectable()
class HttpService:
    def __init__(self, options: HttpModuleOptions | dict[str, Any] = Inject(HTTP_OPTIONS)):
        self.module_options = _normalize_options(options)
        self.client = httpx.AsyncClient(**self._client_options(self.module_options))

    async def request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        request = self.client.build_request(method, url, **kwargs)
        return await self._send_with_retries(request)

    async def request_json(self, method: str, url: str | httpx.URL, **kwargs: Any) -> Any:
        response = await self.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def send(self, request: httpx.Request) -> httpx.Response:
        return await self._send_with_retries(request)

    async def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def get_json(self, url: str | httpx.URL, **kwargs: Any) -> Any:
        return await self.request_json("GET", url, **kwargs)

    async def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def post_json(self, url: str | httpx.URL, **kwargs: Any) -> Any:
        return await self.request_json("POST", url, **kwargs)

    async def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("OPTIONS", url, **kwargs)

    def stream(self, method: str, url: str | httpx.URL, **kwargs: Any):
        return self.client.stream(method, url, **kwargs)

    async def on_application_shutdown(self) -> None:
        await self.client.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _send_with_retries(self, request: httpx.Request) -> httpx.Response:
        request = await self._apply_request_interceptors(request)
        attempts = max(0, self.module_options.retries) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                request = self._clone_request(request)
            try:
                response = await self.client.send(request)
                response = await self._apply_response_interceptors(response)
                if not self._should_retry_response(request, response, attempt, attempts):
                    return response
                await response.aclose()
            except Exception as error:
                intercepted = await self._apply_error_interceptors(error, request)
                if intercepted is not None:
                    return await self._apply_response_interceptors(intercepted)
                last_error = error
                if not self._should_retry_error(request, attempt, attempts):
                    raise
            await self._sleep_before_retry(attempt)
        if last_error is not None:
            raise last_error
        raise RuntimeError("HTTP request failed without a response")

    async def _apply_request_interceptors(self, request: httpx.Request) -> httpx.Request:
        current = request
        for interceptor in self.module_options.request_interceptors:
            result = interceptor(current)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[httpx.Request | None], result)
            if result is not None:
                current = result
        return current

    async def _apply_response_interceptors(self, response: httpx.Response) -> httpx.Response:
        current = response
        for interceptor in self.module_options.response_interceptors:
            result = interceptor(current)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[httpx.Response | None], result)
            if result is not None:
                current = result
        return current

    async def _apply_error_interceptors(
        self,
        error: Exception,
        request: httpx.Request,
    ) -> httpx.Response | None:
        for interceptor in self.module_options.error_interceptors:
            result = interceptor(error, request)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[httpx.Response | None], result)
            if result is not None:
                return result
        return None

    def _should_retry_response(
        self,
        request: httpx.Request,
        response: httpx.Response,
        attempt: int,
        attempts: int,
    ) -> bool:
        return (
            attempt + 1 < attempts
            and request.method.upper() in self.module_options.retry_methods
            and response.status_code in self.module_options.retry_status_codes
        )

    def _should_retry_error(self, request: httpx.Request, attempt: int, attempts: int) -> bool:
        return attempt + 1 < attempts and request.method.upper() in self.module_options.retry_methods

    async def _sleep_before_retry(self, attempt: int) -> None:
        if self.module_options.retry_backoff <= 0:
            return
        await asyncio.sleep(self.module_options.retry_backoff * (2**attempt))

    def _clone_request(self, request: httpx.Request) -> httpx.Request:
        return self.client.build_request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.content,
            extensions=dict(request.extensions),
        )

    def _client_options(self, options: HttpModuleOptions) -> dict[str, Any]:
        client_options = dict(options.client_options)
        if options.base_url is not None:
            client_options["base_url"] = options.base_url
        if options.headers:
            client_options["headers"] = options.headers
        if options.timeout is not None:
            client_options["timeout"] = options.timeout
        return client_options


def _normalize_options(options: HttpModuleOptions | dict[str, Any] | None) -> HttpModuleOptions:
    if options is None:
        return HttpModuleOptions()
    if isinstance(options, HttpModuleOptions):
        return options
    known_keys = {
        "base_url",
        "headers",
        "timeout",
        "retries",
        "retry_status_codes",
        "retry_methods",
        "retry_backoff",
        "request_interceptors",
        "response_interceptors",
        "error_interceptors",
        "client_options",
    }
    explicit = {key: value for key, value in options.items() if key in known_keys and key != "client_options"}
    # An explicit 'client_options' mapping is forwarded verbatim to httpx.AsyncClient;
    # any remaining unknown keys are treated as extra client options and merged in.
    extra_client_options = {key: value for key, value in options.items() if key not in known_keys}
    client_options = {**dict(options.get("client_options") or {}), **extra_client_options}
    return HttpModuleOptions(
        **explicit,
        client_options=client_options,
    )


def _merge_options(
    options: HttpModuleOptions | dict[str, Any] | None,
    overrides: dict[str, Any],
) -> HttpModuleOptions:
    if not overrides:
        return _normalize_options(options)
    if isinstance(options, HttpModuleOptions):
        values = {
            "base_url": options.base_url,
            "headers": options.headers,
            "timeout": options.timeout,
            "retries": options.retries,
            "retry_status_codes": options.retry_status_codes,
            "retry_methods": options.retry_methods,
            "retry_backoff": options.retry_backoff,
            "request_interceptors": options.request_interceptors,
            "response_interceptors": options.response_interceptors,
            "error_interceptors": options.error_interceptors,
            **options.client_options,
            **overrides,
        }
        return _normalize_options(values)
    return _normalize_options({**(options or {}), **overrides})


class HttpModule:
    @staticmethod
    def register(
        options: HttpModuleOptions | dict[str, Any] | None = None,
        *,
        is_global: bool = False,
        **kwargs: Any,
    ) -> type:
        normalized_options = _merge_options(options, kwargs)

        @Module(
            providers=[use_value(HTTP_OPTIONS, normalized_options), HttpService],
            exports=[HttpService],
            global_module=is_global,
        )
        class DynamicHttpModule:
            pass

        return DynamicHttpModule

    @staticmethod
    def register_async(
        *,
        use_factory: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]],
        inject: list[Any] | None = None,
        imports: list[Any] | None = None,
        is_global: bool = False,
    ) -> type:
        async def options_factory(*dependencies: Any) -> HttpModuleOptions:
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[Any], result)
            return _normalize_options(result)

        @Module(
            imports=imports or [],
            providers=[provider_factory(HTTP_OPTIONS, options_factory, inject=inject or []), HttpService],
            exports=[HttpService],
            global_module=is_global,
        )
        class DynamicHttpModule:
            pass

        return DynamicHttpModule
