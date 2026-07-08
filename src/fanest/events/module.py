import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from fanest import Injectable, Module


def OnEvent(event: str):
    def decorator(handler):
        setattr(handler, "__fanest_event__", event)
        return handler

    return decorator


async def _await_all(awaitables: list[Any]) -> None:
    for awaitable in awaitables:
        await awaitable


class _EmitResult:
    """Awaitable returned by :meth:`EventEmitter.emit`.

    Synchronous handlers have already run by the time this object exists, and
    any asynchronous handlers are already scheduled. Awaiting it waits for the
    async handlers to finish; not awaiting it lets them run fire-and-forget.
    Implemented as a generator-based awaitable (not a coroutine) so a bare
    ``emitter.emit(...)`` never raises "coroutine was never awaited".
    """

    __slots__ = ("_tasks",)

    def __init__(self, tasks: list[Any]) -> None:
        self._tasks = tasks

    def __await__(self):
        if self._tasks:
            yield from asyncio.gather(*self._tasks).__await__()


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

    def emit(self, event: str, payload: Any = None) -> _EmitResult:
        """Emit an event to all registered handlers.

        Works whether or not the caller awaits it: sync handlers run inline and
        async handlers are scheduled immediately, so ``emitter.emit(...)`` fires
        the event without silently dropping it, while ``await emitter.emit(...)``
        still waits for the async handlers to complete.
        """
        handlers = [
            *self._handlers.get(event, []),
            *self._handlers.get("*", []),
            *self._once_handlers.pop(event, []),
            *self._once_handlers.pop("*", []),
        ]
        pending: list[Any] = []
        for handler in handlers:
            result = handler(payload)
            if inspect.isawaitable(result):
                pending.append(result)
        if not pending:
            return _EmitResult([])
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            tasks = [loop.create_task(_await_all([awaitable])) for awaitable in pending]
            return _EmitResult(tasks)
        # No running loop: run the async handlers to completion synchronously.
        asyncio.run(_await_all(pending))
        return _EmitResult([])

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
