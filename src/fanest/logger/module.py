import logging
from typing import Any

from fanest import Injectable, Module


@Injectable()
class Logger:
    def __init__(self, context: str | None = None):
        self.context = context or "FaNest"
        self._logger = logging.getLogger(self.context)

    def child(self, context: str) -> "Logger":
        return Logger(context)

    def log(self, message: str, **extra: Any) -> None:
        self._logger.info(message, extra=extra or None)

    def debug(self, message: str, **extra: Any) -> None:
        self._logger.debug(message, extra=extra or None)

    def warn(self, message: str, **extra: Any) -> None:
        self._logger.warning(message, extra=extra or None)

    def error(self, message: str, **extra: Any) -> None:
        self._logger.error(message, extra=extra or None)


class LoggerModule:
    @staticmethod
    def register(*, level: int = logging.INFO) -> type:
        logging.basicConfig(level=level)

        @Module(providers=[Logger], exports=[Logger])
        class DynamicLoggerModule:
            pass

        return DynamicLoggerModule
