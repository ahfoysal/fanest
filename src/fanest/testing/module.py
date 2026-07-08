from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fanest.core.container import FaNestContainer
from fanest.core.factory import FaNestFactory
from fanest.core.providers import use_class, use_factory


class OverrideBuilder:
    def __init__(self, module: "TestingModule", token: Any) -> None:
        self.module = module
        self.token = token

    def use_value(self, value: Any) -> "TestingModule":
        self.module.overrides[self.token] = value
        return self.module

    def use_class(self, cls: type) -> "TestingModule":
        self.module.overrides[self.token] = use_class(self.token, cls)
        return self.module

    def use_factory(self, factory: Any, inject: list[Any] | None = None) -> "TestingModule":
        self.module.overrides[self.token] = use_factory(self.token, factory, inject=inject or [])
        return self.module


@dataclass
class TestingModule:
    __test__ = False

    root_module: type
    overrides: dict[type, Any] = field(default_factory=dict)
    _app: FastAPI | None = None

    @classmethod
    def create(cls, root_module: type) -> "TestingModule":
        return cls(root_module=root_module)

    def override_provider(self, token: type, value: Any) -> "TestingModule":
        self.overrides[token] = value
        return self

    def override(self, token: Any) -> OverrideBuilder:
        return OverrideBuilder(self, token)

    def compile(self) -> FastAPI:
        self._app = FaNestFactory.create(self.root_module, overrides=self.overrides)
        return self._app

    async def compile_async(self) -> FastAPI:
        return self.compile()

    def create_test_client(self) -> TestClient:
        return TestClient(self.compile())

    def get(self, token: Any) -> Any:
        return self._container().resolve(token)

    def resolve(self, token: Any) -> Any:
        container = self._container()
        request_scope = container.begin_request()
        try:
            return container.resolve(token)
        finally:
            container.end_request(request_scope)

    def close(self) -> None:
        self._app = None

    def _container(self) -> FaNestContainer:
        if self._app is None:
            self.compile()
        assert self._app is not None
        return self._app.state.fanest_container


def create_testing_module(root_module: type) -> TestingModule:
    return TestingModule.create(root_module)
