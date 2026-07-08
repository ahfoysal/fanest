import inspect
from collections.abc import Callable
from typing import Any

from fanest import Injectable, Module


def OnEvent(event: str):
    def decorator(handler):
        setattr(handler, "__fanest_event__", event)
        return handler

    return decorator


@Injectable()
class EventEmitter:
    def __init__(self):
        self._handlers: dict[str, list[Callable[..., Any]]] = {}

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def emit(self, event: str, payload: Any = None) -> None:
        for handler in self._handlers.get(event, []):
            result = handler(payload)
            if inspect.isawaitable(result):
                await result


class EventEmitterModule:
    @staticmethod
    def for_root() -> type:
        @Module(providers=[EventEmitter], exports=[EventEmitter])
        class DynamicEventEmitterModule:
            pass

        return DynamicEventEmitterModule
