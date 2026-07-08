from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from fanest.core.container import FaNestContainer
from fanest.core.factory import FaNestFactory


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

    def compile(self) -> FastAPI:
        self._app = FaNestFactory.create(self.root_module, overrides=self.overrides)
        return self._app

    def get(self, token: Any) -> Any:
        return self._container().resolve(token)

    def resolve(self, token: Any) -> Any:
        container = self._container()
        request_scope = container.begin_request()
        try:
            return container.resolve(token)
        finally:
            container.end_request(request_scope)

    def _container(self) -> FaNestContainer:
        if self._app is None:
            self.compile()
        assert self._app is not None
        return self._app.state.fanest_container
