import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fanest import Injectable, Module, Optional, use_value
from fanest.core.metadata import InjectMarker
from fanest.core.providers import token

EVENT_EMITTER_OPTIONS = token("EVENT_EMITTER_OPTIONS")
logger = logging.getLogger("fanest.events")


def OnEvent(event: str, *, priority: int = 0, prepend: bool = False):
    def decorator(handler):
        setattr(handler, "__fanest_event__", event)
        setattr(handler, "__fanest_event_priority__", priority)
        setattr(handler, "__fanest_event_prepend__", prepend)
        return handler

    return decorator


@dataclass(frozen=True)
class EventError:
    event: str
    handler: Callable[..., Any]
    error: BaseException
    payload: Any = None


@dataclass(frozen=True)
class EventEmitterOptions:
    capture_errors: bool = True
    wildcard: bool = True
    max_listeners: int | None = None
    error_handler: Callable[[EventError], Any] | None = None


async def _await_all(awaitables: list[Any]) -> list[Any]:
    return await asyncio.gather(*awaitables, return_exceptions=True)


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
            return (yield from _await_all(self._tasks).__await__())
        return []


@Injectable()
class EventEmitter:
    def __init__(self, options: EventEmitterOptions | None = Optional(EVENT_EMITTER_OPTIONS)):
        self.options = EventEmitterOptions() if options is None or isinstance(options, InjectMarker) else options
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._once_handlers: dict[str, list[Callable[..., Any]]] = {}
        self._priorities: dict[Any, int] = {}
        self._errors: list[EventError] = []

    def on(self, event: str, handler: Callable[..., Any], *, prepend: bool = False, priority: int = 0) -> None:
        handlers = self._handlers.setdefault(event, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            self._enforce_max_listeners(event, len(handlers) + len(self._once_handlers.get(event, [])) + 1)
            self._priorities[self._handler_key(handler)] = priority
            if prepend:
                handlers.insert(0, handler)
            else:
                handlers.append(handler)
            handlers.sort(key=lambda item: self._priorities.get(self._handler_key(item), 0), reverse=True)

    def once(self, event: str, handler: Callable[..., Any], *, prepend: bool = False, priority: int = 0) -> None:
        handlers = self._once_handlers.setdefault(event, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            self._enforce_max_listeners(event, len(handlers) + len(self._handlers.get(event, [])) + 1)
            self._priorities[self._handler_key(handler)] = priority
            if prepend:
                handlers.insert(0, handler)
            else:
                handlers.append(handler)
            handlers.sort(key=lambda item: self._priorities.get(self._handler_key(item), 0), reverse=True)

    def off(self, event: str, handler: Callable[..., Any]) -> None:
        key = self._handler_key(handler)
        self._handlers[event] = [
            item for item in self._handlers.get(event, []) if self._handler_key(item) != key
        ]
        self._once_handlers[event] = [
            item for item in self._once_handlers.get(event, []) if self._handler_key(item) != key
        ]

    def emit(self, event: str, payload: Any = None) -> _EmitResult:
        """Emit an event to all registered handlers.

        Works whether or not the caller awaits it: sync handlers run inline and
        async handlers are scheduled immediately, so ``emitter.emit(...)`` fires
        the event without silently dropping it, while ``await emitter.emit(...)``
        still waits for the async handlers to complete.
        """
        handlers = self._collect_handlers(event)
        pending: list[Any] = []
        for handler in handlers:
            try:
                result = handler(payload)
            except Exception as error:
                error_result = self._capture_error(event, handler, error, payload)
                if inspect.isawaitable(error_result):
                    pending.append(error_result)
                continue
            if inspect.isawaitable(result):
                pending.append(self._guard_async_handler(event, handler, result, payload))
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

    async def emit_async(self, event: str, payload: Any = None) -> list[Any]:
        return await self.emit(event, payload)

    async def emit_strict(self, event: str, payload: Any = None) -> list[Any]:
        previous = len(self._errors)
        results = await self.emit(event, payload)
        captured = self._errors[previous:]
        if captured:
            raise captured[0].error
        return results

    def listeners(self, event: str) -> tuple[Callable[..., Any], ...]:
        return tuple([*self._handlers.get(event, []), *self._once_handlers.get(event, [])])

    def listener_count(self, event: str) -> int:
        return len(self.listeners(event))

    def event_names(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self._handlers.keys(), *self._once_handlers.keys()]))

    def remove_all_listeners(self, event: str | None = None) -> None:
        if event is None:
            self._handlers.clear()
            self._once_handlers.clear()
            return
        self._handlers.pop(event, None)
        self._once_handlers.pop(event, None)

    def errors(self) -> tuple[EventError, ...]:
        return tuple(self._errors)

    def clear_errors(self) -> None:
        self._errors.clear()

    def _collect_handlers(self, event: str) -> list[Callable[..., Any]]:
        handlers: list[Callable[..., Any]] = []
        event_names = self._matching_event_names(event)
        for name in event_names:
            handlers.extend(self._handlers.get(name, []))
        for name in event_names:
            handlers.extend(self._once_handlers.pop(name, []))
        handlers.sort(key=lambda item: self._priorities.get(self._handler_key(item), 0), reverse=True)
        return handlers

    def _matching_event_names(self, event: str) -> list[str]:
        names = [event]
        if not self.options.wildcard:
            return names
        for registered in [*self._handlers.keys(), *self._once_handlers.keys()]:
            if registered == event or registered in names:
                continue
            if registered == "*" or _event_matches(registered, event):
                names.append(registered)
        return names

    async def _guard_async_handler(
        self,
        event: str,
        handler: Callable[..., Any],
        awaitable: Any,
        payload: Any,
    ) -> Any:
        try:
            return await awaitable
        except Exception as error:
            return await self._capture_error_async(event, handler, error, payload)

    def _capture_error(
        self,
        event: str,
        handler: Callable[..., Any],
        error: BaseException,
        payload: Any,
    ) -> Any:
        if not self.options.capture_errors:
            raise error
        event_error = EventError(event=event, handler=handler, error=error, payload=payload)
        self._errors.append(event_error)
        logger.error(
            "Unhandled event listener error for %s",
            event,
            exc_info=(type(error), error, error.__traceback__),
        )
        if self.options.error_handler is None:
            return None
        return self.options.error_handler(event_error)

    async def _capture_error_async(
        self,
        event: str,
        handler: Callable[..., Any],
        error: BaseException,
        payload: Any,
    ) -> Any:
        result = self._capture_error(event, handler, error, payload)
        if inspect.isawaitable(result):
            return await result
        return result

    def _enforce_max_listeners(self, event: str, count: int) -> None:
        if self.options.max_listeners is not None and count > self.options.max_listeners:
            raise RuntimeError(f"Too many listeners registered for event '{event}'")

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


def _event_matches(pattern: str, event: str) -> bool:
    if "*" not in pattern:
        return pattern == event
    pattern_parts = pattern.split(".")
    event_parts = event.split(".")
    if len(pattern_parts) != len(event_parts):
        return False
    return all(pattern_part == "*" or pattern_part == event_part for pattern_part, event_part in zip(pattern_parts, event_parts))


class EventEmitterModule:
    @staticmethod
    def for_root(
        *,
        capture_errors: bool = True,
        wildcard: bool = True,
        max_listeners: int | None = None,
        error_handler: Callable[[EventError], Any] | None = None,
        is_global: bool = False,
    ) -> type:
        options = EventEmitterOptions(
            capture_errors=capture_errors,
            wildcard=wildcard,
            max_listeners=max_listeners,
            error_handler=error_handler,
        )

        @Module(
            providers=[use_value(EVENT_EMITTER_OPTIONS, options), EventEmitter],
            exports=[EventEmitter, EVENT_EMITTER_OPTIONS],
            global_module=is_global,
        )
        class DynamicEventEmitterModule:
            pass

        return DynamicEventEmitterModule
