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
        handlers = self._handlers.setdefault(event, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            handlers.append(handler)

    def once(self, event: str, handler: Callable[..., Any]) -> None:
        handlers = self._once_handlers.setdefault(event, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            handlers.append(handler)

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

    def _handler_key(self, handler: Callable[..., Any]) -> Any:
        return getattr(
            handler,
            "__fanest_registration_key__",
            (
                getattr(getattr(handler, "__self__", None), "__class__", None),
                getattr(getattr(handler, "__func__", None), "__name__", None),
                handler,
            ),
        )


class EventEmitterModule:
    @staticmethod
    def for_root(*, is_global: bool = False) -> type:
        @Module(providers=[EventEmitter], exports=[EventEmitter], global_module=is_global)
        class DynamicEventEmitterModule:
            pass

        return DynamicEventEmitterModule
