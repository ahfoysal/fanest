from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from fanest.core.factory import FaNestFactory


@dataclass
class TestingModule:
    __test__ = False

    root_module: type
    overrides: dict[type, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, root_module: type) -> "TestingModule":
        return cls(root_module=root_module)

    def override_provider(self, token: type, value: Any) -> "TestingModule":
        self.overrides[token] = value
        return self

    def compile(self) -> FastAPI:
        return FaNestFactory.create(self.root_module, overrides=self.overrides)
