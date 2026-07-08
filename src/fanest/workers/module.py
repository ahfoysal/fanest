import inspect
from typing import Any

from fanest import Injectable, Module


def TaskHandler(name: str):
    def decorator(handler):
        setattr(handler, "__fanest_task_handler__", name)
        return handler

    return decorator


@Injectable()
class WorkerService:
    def __init__(self) -> None:
        self._handlers: dict[str, Any] = {}

    def register(self, name: str, handler: Any) -> None:
        self._handlers[name] = handler

    async def run(self, name: str, payload: Any = None) -> Any:
        handler = self._handlers[name]
        result = handler(payload)
        if inspect.isawaitable(result):
            return await result
        return result


class WorkerModule:
    @staticmethod
    def for_root(*, is_global: bool = False) -> type:
        @Module(providers=[WorkerService], exports=[WorkerService], global_module=is_global)
        class DynamicWorkerModule:
            pass

        return DynamicWorkerModule
