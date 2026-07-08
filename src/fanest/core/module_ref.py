from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fanest.core.container import FaNestContainer


class ModuleRef:
    def __init__(self, container: "FaNestContainer"):
        self.container = container

    def get(self, token: Any) -> Any:
        return self.container.resolve(token)

    def resolve(self, token: Any) -> Any:
        request_scope = self.container.begin_request()
        try:
            return self.container.resolve(token)
        finally:
            self.container.end_request(request_scope)

    def create(self, cls: type) -> Any:
        return self.container.instantiate(cls)
