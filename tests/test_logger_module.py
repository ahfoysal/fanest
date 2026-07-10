import io
import json
import logging
import asyncio
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


def test_logger_reregistration_applies_new_stream_and_structured_options():
    first_stream = io.StringIO()
    second_stream = io.StringIO()

    Logger(LoggerOptions(context="ReRegisteredLogger", stream=first_stream, structured=False))
    # Re-registering the same context must apply the new stream/structured config
    # rather than silently reusing the cached logger's handlers.
    reconfigured = Logger(
        LoggerOptions(
            context="ReRegisteredLogger",
            stream=second_stream,
            structured=True,
            include_timestamp=False,
        )
    )

    reconfigured.log("hello", invoice_id="inv_1")
    reconfigured.flush()

    assert first_stream.getvalue() == ""
    payload = json.loads(second_stream.getvalue().splitlines()[0])
    assert payload == {
        "level": "info",
        "message": "hello",
        "context": "ReRegisteredLogger",
        "invoice_id": "inv_1",
    }


def test_logger_module_register_accepts_verbose_level():
    module = LoggerModule.register(level="verbose", context="VerboseLogger")
    options_provider = module.__fanest_module__.providers[0]

    assert options_provider.use_value.level == logging.DEBUG

    logger = Logger(options_provider.use_value)
    assert logger.is_level_enabled("debug")


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


def test_logger_binds_async_local_context_and_reports_exceptions():
    stream = io.StringIO()
    captured: list[tuple[BaseException, dict[str, Any]]] = []
    logger = Logger(
        LoggerOptions(
            context="ReportingLogger",
            structured=True,
            include_timestamp=False,
            stream=stream,
            extra={"service": "orders"},
            exception_reporters=(lambda error, context: captured.append((error, context)),),
        )
    )

    with logger.bind_context(request_id="req-1", tenant="acme"):
        error = RuntimeError("payment failed")
        logger.report_exception(error, order_id="ord_1")

    payload = json.loads(stream.getvalue().splitlines()[0])

    assert payload["level"] == "error"
    assert payload["service"] == "orders"
    assert payload["request_id"] == "req-1"
    assert payload["tenant"] == "acme"
    assert payload["order_id"] == "ord_1"
    assert "RuntimeError" in payload["exception"]
    assert captured == [
        (
            error,
            {
                "service": "orders",
                "request_id": "req-1",
                "tenant": "acme",
                "order_id": "ord_1",
                "logger_context": "ReportingLogger",
            },
        )
    ]
    assert Logger.current_context() == {}


def test_logger_request_context_is_isolated_across_async_tasks():
    async def run_task(request_id: str) -> tuple[str, dict[str, Any]]:
        with Logger.request_context(request_id=request_id):
            await asyncio.sleep(0)
            return request_id, Logger.current_context()

    async def run_all():
        return await asyncio.gather(run_task("req-1"), run_task("req-2"))

    first, second = asyncio.run(run_all())

    assert first == ("req-1", {"request_id": "req-1"})
    assert second == ("req-2", {"request_id": "req-2"})
    assert Logger.current_context() == {}


def test_logger_exception_reporter_failures_do_not_mask_logging():
    stream = io.StringIO()

    def broken_reporter(error: BaseException, context: dict[str, Any]) -> None:
        raise RuntimeError("sentry offline")

    logger = Logger(
        LoggerOptions(
            context="ReporterFailureLogger",
            structured=True,
            include_timestamp=False,
            stream=stream,
            exception_reporters=(broken_reporter,),
        )
    )

    logger.report_exception(ValueError("boom"))

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert lines[0]["message"] == "exception reporter failed"
    assert lines[0]["reporter"] == "broken_reporter"
    assert lines[0]["reporter_error"] == "sentry offline"
    assert lines[1]["level"] == "error"
    assert lines[1]["message"] == "boom"
