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
        self._once_handlers: dict[str, list[Callable[..., Any]]] = {}

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def once(self, event: str, handler: Callable[..., Any]) -> None:
        self._once_handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Callable[..., Any]) -> None:
        self._handlers[event] = [item for item in self._handlers.get(event, []) if item != handler]
        self._once_handlers[event] = [
            item for item in self._once_handlers.get(event, []) if item != handler
        ]

    async def emit(self, event: str, payload: Any = None) -> None:
        handlers = [
            *self._handlers.get(event, []),
            *self._handlers.get("*", []),
            *self._once_handlers.pop(event, []),
            *self._once_handlers.pop("*", []),
        ]
        for handler in handlers:
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
