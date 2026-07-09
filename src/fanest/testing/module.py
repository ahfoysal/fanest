from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fanest.core.container import FaNestContainer
from fanest.core.enhancers import APP_FILTER, APP_GUARD, APP_INTERCEPTOR, APP_PIPE
from fanest.core.factory import FaNestFactory
from fanest.core.providers import use_class, use_factory


class OverrideBuilder:
    def __init__(self, module: "TestingModule", token: Any) -> None:
        self.module = module
        self.token = token

    def use_value(self, value: Any) -> "TestingModule":
        self.module._set_override(self.token, value)
        return self.module

    def use_class(self, cls: type) -> "TestingModule":
        self.module._set_override(self.token, use_class(self.token, cls))
        return self.module

    def use_factory(self, factory: Any, inject: list[Any] | None = None) -> "TestingModule":
        self.module._set_override(self.token, use_factory(self.token, factory, inject=inject or []))
        return self.module


@dataclass
class TestingModule:
    __test__ = False

    root_module: type
    overrides: dict[Any, Any] = field(default_factory=dict)
    _app: FastAPI | None = None
    _client: TestClient | None = None

    @classmethod
    def create(cls, root_module: type) -> "TestingModule":
        return cls(root_module=root_module)

    def override_provider(self, token: type, value: Any) -> "TestingModule":
        self._set_override(token, value)
        return self

    def override(self, token: Any) -> OverrideBuilder:
        return OverrideBuilder(self, token)

    def override_guard(self, token: Any = APP_GUARD) -> OverrideBuilder:
        return self.override(token)

    def override_interceptor(self, token: Any = APP_INTERCEPTOR) -> OverrideBuilder:
        return self.override(token)

    def override_filter(self, token: Any = APP_FILTER) -> OverrideBuilder:
        return self.override(token)

    def override_pipe(self, token: Any = APP_PIPE) -> OverrideBuilder:
        return self.override(token)

    def override_controller(self, token: Any) -> OverrideBuilder:
        return self.override(token)

    def compile(self) -> FastAPI:
        self._close_client()
        self._app = FaNestFactory.create(self.root_module, overrides=self.overrides)
        return self._app

    async def compile_async(self) -> FastAPI:
        self._close_client()
        self._app = await FaNestFactory.create_async(self.root_module, overrides=self.overrides)
        return self._app

    def create_test_client(self) -> TestClient:
        if self._client is None:
            self._client = TestClient(self.create_application())
        assert self._client is not None
        return self._client

    def create_client(self) -> TestClient:
        return self.create_test_client()

    def create_application(self) -> FastAPI:
        if self._app is None:
            return self.compile()
        return self._app

    def get(self, token: Any) -> Any:
        return self._container().resolve(token)

    async def get_async(self, token: Any) -> Any:
        return await self._container().resolve_async(token)

    def resolve(self, token: Any) -> Any:
        container = self._container()
        request_scope = container.begin_request()
        try:
            return container.resolve(token)
        finally:
            container.end_request(request_scope)

    async def resolve_async(self, token: Any) -> Any:
        container = self._container()
        request_scope = container.begin_request()
        try:
            return await container.resolve_async(token)
        finally:
            container.end_request(request_scope)

    def close(self) -> None:
        self._close_client()
        self._app = None

    def __enter__(self) -> "TestingModule":
        if self._app is None:
            self.compile()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _container(self) -> FaNestContainer:
        if self._app is None:
            self.compile()
        assert self._app is not None
        return self._app.state.fanest_container

    def _close_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _set_override(self, token: Any, value: Any) -> None:
        self.overrides[token] = value
        self.close()


def create_testing_module(root_module: type) -> TestingModule:
    return TestingModule.create(root_module)
