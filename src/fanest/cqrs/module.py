import inspect
from typing import Any

from fanest import Injectable, Module


def CommandHandler(command: type):
    def decorator(cls):
        setattr(cls, "__fanest_command_handler__", command)
        return Injectable()(cls)

    return decorator


def QueryHandler(query: type):
    def decorator(cls):
        setattr(cls, "__fanest_query_handler__", query)
        return Injectable()(cls)

    return decorator


def EventsHandler(event: type):
    def decorator(cls):
        events = list(getattr(cls, "__fanest_event_handlers__", []))
        events.append(event)
        setattr(cls, "__fanest_event_handlers__", events)
        return Injectable()(cls)

    return decorator


@Injectable()
class CommandBus:
    def __init__(self) -> None:
        self._handlers: dict[type, Any] = {}

    def register(self, command: type, handler: Any) -> None:
        self._handlers[command] = handler

    async def execute(self, command: Any) -> Any:
        handler = self._handlers[type(command)]
        result = handler.execute(command)
        if inspect.isawaitable(result):
            return await result
        return result


@Injectable()
class QueryBus:
    def __init__(self) -> None:
        self._handlers: dict[type, Any] = {}

    def register(self, query: type, handler: Any) -> None:
        self._handlers[query] = handler

    async def execute(self, query: Any) -> Any:
        handler = self._handlers[type(query)]
        result = handler.execute(query)
        if inspect.isawaitable(result):
            return await result
        return result


@Injectable()
class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Any]] = {}

    def register(self, event: type, handler: Any) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def publish(self, event: Any) -> None:
        for handler in self._handlers.get(type(event), []):
            result = handler.handle(event)
            if inspect.isawaitable(result):
                await result


class CqrsModule:
    @staticmethod
    def for_root(*, is_global: bool = False) -> type:
        @Module(
            providers=[CommandBus, QueryBus, EventBus],
            exports=[CommandBus, QueryBus, EventBus],
            global_module=is_global,
        )
        class DynamicCqrsModule:
            pass

        return DynamicCqrsModule
