from dataclasses import dataclass, field
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fanest.core.container import FaNestContainer
from fanest.core.enhancers import APP_FILTER, APP_GUARD, APP_INTERCEPTOR, APP_PIPE
from fanest.core.factory import FaNestFactory
from fanest.core.metadata import (
    ClassProvider,
    ExistingProvider,
    FactoryProvider,
    InjectMarker,
    ValueProvider,
)
from fanest.core.module import dynamic_module
from fanest.core.providers import use_class, use_factory, use_value


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
    mocker: Any | None = None
    _auto_mock_tokens: set[Any] = field(default_factory=set)
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

    def use_mocker(self, mocker: Any = None) -> "TestingModule":
        self.mocker = mocker or create_auto_mock
        self.close()
        return self

    def compile(self) -> FastAPI:
        self._close_client()
        self._apply_auto_mocks()
        self._app = FaNestFactory.create(self._root_for_compile(), overrides=self.overrides)
        return self._app

    async def compile_async(self) -> FastAPI:
        self._close_client()
        self._apply_auto_mocks()
        self._app = await FaNestFactory.create_async(
            self._root_for_compile(),
            overrides=self.overrides,
        )
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

    def _apply_auto_mocks(self) -> None:
        if self.mocker is None:
            return
        known = set(self.overrides)
        metadata = getattr(self.root_module, "__fanest_module__", None)
        if metadata is None:
            return
        for provider in metadata.providers:
            known.add(_provider_token(provider))
        targets = [*metadata.controllers, *metadata.providers]
        for target in targets:
            target_type = _provider_type(target)
            if target_type is None:
                continue
            for dependency in _constructor_dependencies(target_type):
                if dependency in known:
                    continue
                mock = self.mocker(dependency)
                if mock is None:
                    continue
                self.overrides[dependency] = mock
                self._auto_mock_tokens.add(dependency)
                known.add(dependency)

    def _root_for_compile(self) -> Any:
        if not self._auto_mock_tokens:
            return self.root_module
        metadata = getattr(self.root_module, "__fanest_module__", None)
        if metadata is None:
            return self.root_module
        mock_providers = [
            use_value(token, self.overrides[token])
            for token in self._auto_mock_tokens
            if token in self.overrides
        ]
        return dynamic_module(
            self.root_module,
            imports=metadata.imports,
            controllers=metadata.controllers,
            providers=[*metadata.providers, *mock_providers],
            gateways=metadata.gateways,
            middlewares=metadata.middlewares,
            exports=metadata.exports,
            global_=metadata.global_module,
        )


def create_auto_mock(token: Any) -> Any:
    if inspect.isclass(token):
        mock = MagicMock(spec=token)
        for name, member in inspect.getmembers(token):
            if inspect.iscoroutinefunction(member):
                setattr(mock, name, AsyncMock())
        return mock
    return MagicMock(name=str(token))


def create_testing_module(root_module: type) -> TestingModule:
    return TestingModule.create(root_module)


def _provider_token(provider: Any) -> Any:
    if isinstance(provider, (ClassProvider, ValueProvider, FactoryProvider, ExistingProvider)):
        return provider.provide
    return provider


def _provider_type(provider: Any) -> type | None:
    if inspect.isclass(provider):
        return provider
    if isinstance(provider, ClassProvider):
        return provider.use_class
    return None


def _constructor_dependencies(provider: type) -> list[Any]:
    try:
        signature = inspect.signature(provider.__init__)
    except (TypeError, ValueError):
        return []
    try:
        type_hints = inspect.get_annotations(provider.__init__, eval_str=True)
    except Exception:
        type_hints = inspect.get_annotations(provider.__init__, eval_str=False)
    dependencies: list[Any] = []
    for name, parameter in signature.parameters.items():
        if name == "self":
            continue
        default = parameter.default
        if isinstance(default, InjectMarker):
            dependencies.append(default.token)
            continue
        if default is not inspect.Parameter.empty:
            continue
        annotation = type_hints.get(name, parameter.annotation)
        if annotation is inspect.Parameter.empty:
            continue
        dependencies.append(annotation)
    return dependencies
