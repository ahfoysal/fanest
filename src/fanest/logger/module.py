import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from json import dumps
from typing import Any, TextIO

from fanest import Injectable, Module, Optional, use_value
from fanest.core.metadata import InjectMarker
from fanest.core.providers import token

LOGGER_OPTIONS = token("LOGGER_OPTIONS")


class LogLevel(IntEnum):
    DEBUG = logging.DEBUG
    LOG = logging.INFO
    WARN = logging.WARNING
    ERROR = logging.ERROR
    FATAL = logging.CRITICAL


@dataclass(frozen=True)
class LoggerOptions:
    level: int | str = logging.INFO
    context: str = "FaNest"
    structured: bool = False
    include_timestamp: bool = True
    propagate: bool = False
    handlers: tuple[logging.Handler, ...] = ()
    stream: TextIO | None = None
    extra: dict[str, Any] | None = None


class StructuredLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "context": record.name,
        }
        base_extra = getattr(record, "fanest_base_extra", None)
        if base_extra:
            payload.update(base_extra)
        fanest_context = getattr(record, "fanest_context", None)
        if fanest_context:
            payload["context"] = fanest_context
        fanest_extra = getattr(record, "fanest_extra", None)
        if fanest_extra:
            payload.update(fanest_extra)
        if getattr(record, "fanest_timestamp", True):
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return dumps(payload, default=str, separators=(",", ":"))


@Injectable()
class Logger:
    def __init__(self, options: LoggerOptions | None = Optional(LOGGER_OPTIONS)):
        self.options = LoggerOptions() if options is None or isinstance(options, InjectMarker) else options
        self.context = self.options.context
        self._logger = logging.getLogger(self.options.context)
        self._configure_logger(self._logger, self.options)

    def child(self, context: str) -> "Logger":
        return Logger(LoggerOptions(**{**self.options.__dict__, "context": context}))

    def with_context(self, context: str) -> "Logger":
        return self.child(context)

    def set_context(self, context: str) -> None:
        self.context = context
        self._logger = logging.getLogger(context)
        self._configure_logger(self._logger, self.options)

    def set_level(self, level: int | str) -> None:
        coerced = _coerce_level(level)
        self._logger.setLevel(coerced)
        for handler in self._logger.handlers:
            handler.setLevel(coerced)

    def is_level_enabled(self, level: int | str) -> bool:
        return self._logger.isEnabledFor(_coerce_level(level))

    def add_handler(self, handler: logging.Handler) -> None:
        handler.setLevel(self._logger.level)
        if handler.formatter is None:
            handler.setFormatter(self._formatter(self.options))
        self._logger.addHandler(handler)

    def remove_handlers(self) -> None:
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()

    def flush(self) -> None:
        for handler in self._logger.handlers:
            handler.flush()

    def log(self, message: str, **extra: Any) -> None:
        self._write(logging.INFO, message, extra)

    def debug(self, message: str, **extra: Any) -> None:
        self._write(logging.DEBUG, message, extra)

    def verbose(self, message: str, **extra: Any) -> None:
        self.debug(message, **extra)

    def warn(self, message: str, **extra: Any) -> None:
        self._write(logging.WARNING, message, extra)

    def error(self, message: str, exc_info: Any = None, **extra: Any) -> None:
        self._write(logging.ERROR, message, extra, exc_info=exc_info)

    def fatal(self, message: str, exc_info: Any = None, **extra: Any) -> None:
        self._write(logging.CRITICAL, message, extra, exc_info=exc_info)

    def _write(self, level: int, message: str, extra: dict[str, Any], exc_info: Any = None) -> None:
        self._logger.log(
            level,
            message,
            exc_info=exc_info,
            extra={
                "fanest_context": self.context,
                "fanest_base_extra": self.options.extra or {},
                "fanest_extra": extra,
                "fanest_timestamp": self.options.include_timestamp,
            },
        )

    def _configure_logger(self, logger: logging.Logger, options: LoggerOptions) -> None:
        logger.setLevel(_coerce_level(options.level))
        logger.propagate = options.propagate
        if logger.handlers:
            for handler in logger.handlers:
                handler.setLevel(_coerce_level(options.level))
                if handler.formatter is None:
                    handler.setFormatter(self._formatter(options))
            return
        handlers = list(options.handlers) or [logging.StreamHandler(options.stream or sys.stderr)]
        for handler in handlers:
            handler.setLevel(_coerce_level(options.level))
            if handler.formatter is None:
                handler.setFormatter(self._formatter(options))
            logger.addHandler(handler)

    def _formatter(self, options: LoggerOptions) -> logging.Formatter:
        if options.structured:
            return StructuredLogFormatter()
        return logging.Formatter("%(levelname)s [%(name)s] %(message)s")


class LoggerModule:
    @staticmethod
    def register(
        *,
        level: int | str = logging.INFO,
        context: str = "FaNest",
        structured: bool = False,
        include_timestamp: bool = True,
        propagate: bool = False,
        handlers: list[logging.Handler] | tuple[logging.Handler, ...] | None = None,
        stream: TextIO | None = None,
        extra: dict[str, Any] | None = None,
        is_global: bool = False,
    ) -> type:
        options = LoggerOptions(
            level=_coerce_level(level),
            context=context,
            structured=structured,
            include_timestamp=include_timestamp,
            propagate=propagate,
            handlers=tuple(handlers or ()),
            stream=stream,
            extra=extra,
        )

        @Module(
            providers=[use_value(LOGGER_OPTIONS, options), Logger],
            exports=[Logger, LOGGER_OPTIONS],
            global_module=is_global,
        )
        class DynamicLoggerModule:
            pass

        return DynamicLoggerModule


def _coerce_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    normalized = level.strip().upper()
    if normalized == "LOG":
        return logging.INFO
    if normalized in logging._nameToLevel:
        return logging._nameToLevel[normalized]
    raise ValueError(f"Unknown log level: {level}")
