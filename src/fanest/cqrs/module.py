import inspect
import logging
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, cast

from fanest import Injectable, Module, Optional, use_value
from fanest.core.metadata import InjectMarker
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

CQRS_OPTIONS = token("CQRS_OPTIONS")
logger = logging.getLogger("fanest.cqrs")


@dataclass(frozen=True)
class CqrsOptions:
    rethrow_unhandled_exceptions: bool = False
    allow_subclass_handlers: bool = True


@dataclass(frozen=True)
class CqrsUnhandledException:
    source: str
    message: Any
    error: BaseException
    handler: Any | None = None


class CqrsHandlerNotFoundError(LookupError):
    def __init__(self, bus_name: str, message_type: type) -> None:
        self.bus_name = bus_name
        self.message_type = message_type
        super().__init__(
            f"No {bus_name} handler registered for {message_type.__module__}.{message_type.__qualname__}."
        )


class CqrsHandlerError(RuntimeError):
    def __init__(self, source: str, message: Any, handler: Any, error: BaseException) -> None:
        self.source = source
        self.message = message
        self.handler = handler
        self.error = error
        super().__init__(f"{source} handler {handler!r} failed for {type(message).__qualname__}: {error}")


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


def Saga(event: type | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_cqrs_saga__", event or object)
        return handler

    return decorator


@Injectable()
class UnhandledExceptionBus:
    def __init__(self) -> None:
        self._events: list[CqrsUnhandledException] = []
        self._subscribers: list[Callable[[CqrsUnhandledException], Any]] = []

    def publish(self, exception: CqrsUnhandledException) -> None:
        self._events.append(exception)
        for subscriber in list(self._subscribers):
            result = subscriber(exception)
            if inspect.isawaitable(result):
                raise RuntimeError("Async unhandled exception subscribers must use publish_async().")

    async def publish_async(self, exception: CqrsUnhandledException) -> None:
        self._events.append(exception)
        for subscriber in list(self._subscribers):
            result = subscriber(exception)
            if inspect.isawaitable(result):
                await result

    def subscribe(self, subscriber: Callable[[CqrsUnhandledException], Any]) -> None:
        if subscriber not in self._subscribers:
            self._subscribers.append(subscriber)

    def events(self) -> tuple[CqrsUnhandledException, ...]:
        return tuple(self._events)

    def clear(self) -> None:
        self._events.clear()


@Injectable()
class CommandBus:
    def __init__(
        self,
        options: CqrsOptions | None = Optional(CQRS_OPTIONS),
        exceptions: UnhandledExceptionBus | None = Optional(UnhandledExceptionBus),
    ) -> None:
        self.options = _normalize_options(options)
        self.exceptions = None if isinstance(exceptions, InjectMarker) else exceptions
        self._handlers: dict[type, Any] = {}

    def register(self, command: type, handler: Any) -> None:
        self._handlers[command] = handler

    async def execute(self, command: Any) -> Any:
        command_type = type(command)
        handler = self._handler_for(command_type)
        if handler is None:
            raise CqrsHandlerNotFoundError("command", command_type)
        try:
            result = handler.execute(command)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as error:
            await self._capture("command", command, handler, error)
            return None

    def _handler_for(self, message_type: type) -> Any | None:
        handler = self._handlers.get(message_type)
        if handler is not None or not self.options.allow_subclass_handlers:
            return handler
        for registered_type, candidate in self._handlers.items():
            if issubclass(message_type, registered_type):
                return candidate
        return None

    async def _capture(self, source: str, message: Any, handler: Any, error: BaseException) -> None:
        if self.options.rethrow_unhandled_exceptions:
            raise error
        if self.exceptions is not None:
            await self.exceptions.publish_async(CqrsUnhandledException(source, message, error, handler))
        logger.exception("%s handler failed", source)


@Injectable()
class QueryBus:
    def __init__(
        self,
        options: CqrsOptions | None = Optional(CQRS_OPTIONS),
        exceptions: UnhandledExceptionBus | None = Optional(UnhandledExceptionBus),
    ) -> None:
        self.options = _normalize_options(options)
        self.exceptions = None if isinstance(exceptions, InjectMarker) else exceptions
        self._handlers: dict[type, Any] = {}

    def register(self, query: type, handler: Any) -> None:
        self._handlers[query] = handler

    async def execute(self, query: Any) -> Any:
        query_type = type(query)
        handler = self._handler_for(query_type)
        if handler is None:
            raise CqrsHandlerNotFoundError("query", query_type)
        try:
            result = handler.execute(query)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as error:
            await self._capture("query", query, handler, error)
            return None

    def _handler_for(self, message_type: type) -> Any | None:
        handler = self._handlers.get(message_type)
        if handler is not None or not self.options.allow_subclass_handlers:
            return handler
        for registered_type, candidate in self._handlers.items():
            if issubclass(message_type, registered_type):
                return candidate
        return None

    async def _capture(self, source: str, message: Any, handler: Any, error: BaseException) -> None:
        if self.options.rethrow_unhandled_exceptions:
            raise error
        if self.exceptions is not None:
            await self.exceptions.publish_async(CqrsUnhandledException(source, message, error, handler))
        logger.exception("%s handler failed", source)


@Injectable()
class EventBus:
    def __init__(
        self,
        command_bus: CommandBus | None = Optional(CommandBus),
        options: CqrsOptions | None = Optional(CQRS_OPTIONS),
        exceptions: UnhandledExceptionBus | None = Optional(UnhandledExceptionBus),
    ) -> None:
        self.command_bus = None if isinstance(command_bus, InjectMarker) else command_bus
        self.options = _normalize_options(options)
        self.exceptions = None if isinstance(exceptions, InjectMarker) else exceptions
        self._handlers: dict[type, list[Any]] = {}
        self._sagas: dict[type, list[Callable[[Any], Any]]] = {}

    def register(self, event: type, handler: Any) -> None:
        handlers = self._handlers.setdefault(event, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            handlers.append(handler)

    async def publish(self, event: Any) -> None:
        for handler in self._handlers_for(type(event)):
            try:
                result = handler.handle(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                await self._capture("event", event, handler, error)
        await self._run_sagas(event)

    def register_saga(self, event: type, handler: Callable[[Any], Any]) -> None:
        handlers = self._sagas.setdefault(event, [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            handlers.append(handler)

    def _handlers_for(self, message_type: type) -> list[Any]:
        handlers = [*self._handlers.get(message_type, [])]
        if self.options.allow_subclass_handlers:
            for registered_type, candidates in self._handlers.items():
                if registered_type is not message_type and issubclass(message_type, registered_type):
                    handlers.extend(candidates)
        deduped: list[Any] = []
        seen: set[Any] = set()
        for handler in handlers:
            key = self._handler_key(handler)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(handler)
        return deduped

    def _sagas_for(self, message_type: type) -> list[Callable[[Any], Any]]:
        sagas = [*self._sagas.get(message_type, []), *self._sagas.get(object, [])]
        if not self.options.allow_subclass_handlers:
            return sagas
        for registered_type, candidates in self._sagas.items():
            if registered_type not in {message_type, object} and issubclass(message_type, registered_type):
                sagas.extend(candidates)
        return sagas

    async def _run_sagas(self, event: Any) -> None:
        for saga in self._sagas_for(type(event)):
            try:
                commands = saga(event)
                if inspect.isawaitable(commands):
                    commands = await commands
                await self._dispatch_saga_commands(commands)
            except Exception as error:
                await self._capture("saga", event, saga, error)

    async def _dispatch_saga_commands(self, commands: Any) -> None:
        if commands is None or self.command_bus is None:
            return
        if isinstance(commands, AsyncIterable):
            async for command in commands:
                await self.command_bus.execute(command)
            return
        if isinstance(commands, Iterable) and not isinstance(commands, (str, bytes, dict)):
            for command in commands:
                await self.command_bus.execute(command)
            return
        await self.command_bus.execute(commands)

    async def _capture(self, source: str, message: Any, handler: Any, error: BaseException) -> None:
        if self.options.rethrow_unhandled_exceptions:
            raise error
        if self.exceptions is not None:
            await self.exceptions.publish_async(CqrsUnhandledException(source, message, error, handler))
        logger.exception("%s handler failed", source)

    def _handler_key(self, handler: Any) -> Any:
        return getattr(handler, "__fanest_registration_key__", handler)


@Injectable()
class EventPublisher:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus

    async def publish(self, event: Any) -> None:
        await self.event_bus.publish(event)

    async def publish_all(self, events: Iterable[Any]) -> None:
        for event in events:
            await self.event_bus.publish(event)

    def merge_object_context(self, model: Any) -> Any:
        return self.merge_context(model)

    def merge_class_context(self, model_cls: type) -> type:
        publisher = self

        class PublishedModel(model_cls):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                publisher.merge_context(self)

        PublishedModel.__name__ = model_cls.__name__
        PublishedModel.__qualname__ = model_cls.__qualname__
        PublishedModel.__module__ = model_cls.__module__
        return PublishedModel

    def merge_context(self, model: Any) -> Any:
        publisher = self
        if hasattr(model, "_publisher"):
            setattr(model, "_publisher", publisher)

        async def publish(event: Any) -> None:
            await publisher.publish(event)

        async def commit() -> None:
            for event in list(getattr(model, "events", [])):
                await publisher.publish(event)
            clear_events = getattr(model, "clear_events", None)
            if clear_events is not None:
                clear_events()
            elif hasattr(model, "events"):
                getattr(model, "events").clear()

        setattr(model, "publish", publish)
        setattr(model, "commit", commit)
        return model


class AggregateRoot:
    def __init__(self, *, auto_commit: bool = False) -> None:
        self.auto_commit = auto_commit
        self._events: list[Any] = []
        self._publisher: EventPublisher | None = None

    @property
    def events(self) -> list[Any]:
        return self._events

    def apply(self, event: Any, *, is_from_history: bool = False) -> Any:
        result = self._apply_event(event)
        if inspect.isawaitable(result):
            raise RuntimeError("Async aggregate event appliers require apply_async().")
        if is_from_history:
            return event
        if self.auto_commit and self._publisher is not None:
            result = self._publisher.publish(event)
            if inspect.isawaitable(result):
                raise RuntimeError("Async event publishing requires await aggregate.commit().")
            return result
        self._events.append(event)
        return event

    async def apply_async(self, event: Any, *, is_from_history: bool = False) -> Any:
        result = self._apply_event(event)
        if inspect.isawaitable(result):
            await result
        if is_from_history:
            return event
        if self.auto_commit and self._publisher is not None:
            await self._publisher.publish(event)
            return event
        self._events.append(event)
        return event

    async def publish(self, event: Any) -> None:
        if self._publisher is None:
            raise RuntimeError("AggregateRoot is not attached to an EventPublisher.")
        await self._publisher.publish(event)

    async def commit(self) -> None:
        if self._publisher is None:
            raise RuntimeError("AggregateRoot is not attached to an EventPublisher.")
        await self._publisher.publish_all(tuple(self._events))
        self.clear_events()

    def uncommit(self) -> None:
        self.clear_events()

    def clear_events(self) -> None:
        self._events.clear()

    def get_uncommitted_events(self) -> tuple[Any, ...]:
        return tuple(self._events)

    def load_from_history(self, events: Iterable[Any]) -> None:
        for event in events:
            self.apply(event, is_from_history=True)

    def _apply_event(self, event: Any) -> Any:
        handler = getattr(self, f"on_{type(event).__name__}", None)
        if handler is None:
            handler = getattr(self, "on_event", None)
        if handler is not None:
            return handler(event)
        return None


def _normalize_options(options: CqrsOptions | dict[str, Any] | None) -> CqrsOptions:
    if options is None or isinstance(options, InjectMarker):
        return CqrsOptions()
    if isinstance(options, dict):
        return CqrsOptions(
            rethrow_unhandled_exceptions=options.get("rethrow_unhandled_exceptions", False),
            allow_subclass_handlers=options.get("allow_subclass_handlers", True),
        )
    return options


class CqrsModule:
    _root_modules: dict[tuple[bool, bool, bool], type] = {}

    @staticmethod
    def for_root(
        *,
        is_global: bool = False,
        rethrow_unhandled_exceptions: bool = False,
        allow_subclass_handlers: bool = True,
    ) -> type:
        cache_key = (is_global, rethrow_unhandled_exceptions, allow_subclass_handlers)
        if cache_key in CqrsModule._root_modules:
            return CqrsModule._root_modules[cache_key]
        options = CqrsOptions(
            rethrow_unhandled_exceptions=rethrow_unhandled_exceptions,
            allow_subclass_handlers=allow_subclass_handlers,
        )

        @Module(
            providers=[
                use_value(CQRS_OPTIONS, options),
                UnhandledExceptionBus,
                CommandBus,
                QueryBus,
                EventBus,
                EventPublisher,
            ],
            exports=[
                CommandBus,
                QueryBus,
                EventBus,
                EventPublisher,
                UnhandledExceptionBus,
                CQRS_OPTIONS,
            ],
            global_module=is_global,
        )
        class DynamicCqrsModule:
            pass

        CqrsModule._root_modules[cache_key] = DynamicCqrsModule
        return DynamicCqrsModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., CqrsOptions | dict[str, Any] | Awaitable[CqrsOptions | dict[str, Any]]],
        inject: list[Any] | None = None,
        imports: list[Any] | None = None,
        is_global: bool = False,
    ) -> type:
        async def options_factory(*dependencies: Any) -> CqrsOptions:
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[Any], result)
            return _normalize_options(result)

        @Module(
            imports=imports or [],
            providers=[
                provider_factory(CQRS_OPTIONS, options_factory, inject=inject or []),
                UnhandledExceptionBus,
                CommandBus,
                QueryBus,
                EventBus,
                EventPublisher,
            ],
            exports=[
                CommandBus,
                QueryBus,
                EventBus,
                EventPublisher,
                UnhandledExceptionBus,
                CQRS_OPTIONS,
            ],
            global_module=is_global,
        )
        class DynamicCqrsModule:
            pass

        return DynamicCqrsModule
