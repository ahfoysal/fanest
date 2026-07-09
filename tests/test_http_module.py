import httpx
import pytest

from fanest import FaNestFactory, Injectable, Module
from fanest.http import HttpModule, HttpModuleOptions, HttpService


@pytest.mark.anyio
async def test_http_module_applies_base_headers_interceptors_and_retry():
    attempts = 0
    seen_headers: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        seen_headers.append(request.headers["x-request-id"])
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    def request_interceptor(request: httpx.Request) -> httpx.Request:
        request.headers["x-request-id"] = "abc"
        return request

    def response_interceptor(response: httpx.Response) -> httpx.Response:
        response.headers["x-seen"] = "yes"
        return response

    @Module(
        imports=[
            HttpModule.register(
                base_url="https://api.example.test",
                headers={"x-default": "fanest"},
                transport=httpx.MockTransport(handler),
                retries=1,
                request_interceptors=[request_interceptor],
                response_interceptors=[response_interceptor],
            )
        ]
    )
    class AppModule:
        pass

    app = FaNestFactory.create(AppModule)
    service = app.state.fanest_container.resolve(HttpService)

    response = await service.get("/users")

    assert response.json() == {"ok": True}
    assert response.headers["x-seen"] == "yes"
    assert attempts == 2
    assert seen_headers == ["abc", "abc"]
    await service.aclose()


@pytest.mark.anyio
async def test_http_error_interceptor_can_return_fallback_response():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    def error_interceptor(error: Exception, request: httpx.Request) -> httpx.Response:
        assert isinstance(error, httpx.ConnectError)
        return httpx.Response(200, json={"fallback": True}, request=request)

    service = HttpService(
        HttpModuleOptions(
            base_url="https://api.example.test",
            client_options={"transport": httpx.MockTransport(handler)},
            error_interceptors=[error_interceptor],
        )
    )

    response = await service.get("/health")

    assert response.json() == {"fallback": True}
    await service.aclose()


@pytest.mark.anyio
async def test_http_register_async_supports_async_options_factory():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": str(request.url)}, request=request)

    @Injectable()
    class HttpConfig:
        async def options(self) -> dict:
            return {
                "base_url": "https://async.example.test",
                "transport": httpx.MockTransport(handler),
            }

    async def options_factory(config: HttpConfig) -> dict:
        return await config.options()

    @Module(providers=[HttpConfig], exports=[HttpConfig])
    class ConfigModule:
        pass

    @Module(
        imports=[HttpModule.register_async(use_factory=options_factory, inject=[HttpConfig], imports=[ConfigModule])],
    )
    class AppModule:
        pass

    app = await FaNestFactory.create_async(AppModule)
    service = await app.state.fanest_container.resolve_async(HttpService)

    response = await service.get("/ready")

    assert response.json() == {"url": "https://async.example.test/ready"}
    await service.aclose()
