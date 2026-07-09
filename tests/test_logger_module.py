import io
import json
import logging
from typing import Any, cast

from fanest.logger import Logger, LoggerModule, LoggerOptions


def test_logger_supports_structured_context_extra_and_runtime_level_changes():
    stream = io.StringIO()
    logger = Logger(
        LoggerOptions(
            context="TestLoggerStructured",
            structured=True,
            include_timestamp=False,
            stream=stream,
            extra={"service": "billing"},
        )
    )
    child = logger.child("BillingService")

    child.log("invoice created", invoice_id="inv_1")
    logger.set_level("error")
    child.debug("hidden")
    child.error("failed", reason="timeout")
    logger.flush()

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert lines == [
        {
            "level": "info",
            "message": "invoice created",
            "context": "BillingService",
            "service": "billing",
            "invoice_id": "inv_1",
        },
        {
            "level": "error",
            "message": "failed",
            "context": "BillingService",
            "service": "billing",
            "reason": "timeout",
        },
    ]
    assert not logger.is_level_enabled("debug")


def test_logger_can_use_custom_handlers_and_remove_them():
    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = ListHandler()
    logger = Logger(LoggerOptions(context="TestLoggerHandlers", handlers=(handler,)))

    logger.warn("careful", code="slow")
    logger.remove_handlers()
    logger.error("dropped")

    assert len(records) == 1
    assert records[0].getMessage() == "careful"
    assert cast(Any, records[0]).fanest_extra == {"code": "slow"}


def test_logger_module_register_exports_configurable_options():
    module = LoggerModule.register(
        context="ConfiguredLogger",
        level="warn",
        structured=True,
        include_timestamp=False,
        extra={"app": "fanest"},
    )
    metadata = module.__fanest_module__
    options_provider = metadata.providers[0]

    assert options_provider.use_value.context == "ConfiguredLogger"
    assert options_provider.use_value.level == logging.WARNING
    assert options_provider.use_value.structured is True
    assert options_provider.use_value.extra == {"app": "fanest"}
