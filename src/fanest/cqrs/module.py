import inspect
from typing import Any

from fanest import Injectable, Module


class CqrsHandlerNotFoundError(LookupError):
    def __init__(self, bus_name: str, message_type: type) -> None:
        self.bus_name = bus_name
        self.message_type = message_type
        super().__init__(
            f"No {bus_name} handler registered for {message_type.__module__}.{message_type.__qualname__}."
        )


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
        command_type = type(command)
        handler = self._handlers.get(command_type)
        if handler is None:
            raise CqrsHandlerNotFoundError("command", command_type)
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
        query_type = type(query)
        handler = self._handlers.get(query_type)
        if handler is None:
            raise CqrsHandlerNotFoundError("query", query_type)
        result = handler.execute(query)
        if inspect.isawaitable(result):
            return await result
        return result


@Injectable()
class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type, list[Any]] = {}

    def register(self, event: type, handler: Any) -> None:
        handlers = self._handlers.setdefault(event, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            handlers.append(handler)

    async def publish(self, event: Any) -> None:
        for handler in self._handlers.get(type(event), []):
            result = handler.handle(event)
            if inspect.isawaitable(result):
                await result

    def _handler_key(self, handler: Any) -> Any:
        return getattr(handler, "__fanest_registration_key__", handler)


class CqrsModule:
    _root_modules: dict[bool, type] = {}

    @staticmethod
    def for_root(*, is_global: bool = False) -> type:
        if is_global in CqrsModule._root_modules:
            return CqrsModule._root_modules[is_global]

        @Module(
            providers=[CommandBus, QueryBus, EventBus],
            exports=[CommandBus, QueryBus, EventBus],
            global_module=is_global,
        )
        class DynamicCqrsModule:
            pass

        CqrsModule._root_modules[is_global] = DynamicCqrsModule
        return DynamicCqrsModule
