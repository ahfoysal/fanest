from collections.abc import Callable
from typing import Any


class CronExpression:
    EVERY_SECOND = "*/1 * * * * *"
    EVERY_5_SECONDS = "*/5 * * * * *"
    EVERY_10_SECONDS = "*/10 * * * * *"
    EVERY_30_SECONDS = "*/30 * * * * *"
    EVERY_MINUTE = "*/1 * * * *"
    EVERY_5_MINUTES = "*/5 * * * *"
    EVERY_10_MINUTES = "*/10 * * * *"
    EVERY_30_MINUTES = "*/30 * * * *"
    EVERY_HOUR = "0 * * * *"
    EVERY_DAY_AT_MIDNIGHT = "0 0 * * *"


def Interval(seconds: float, name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_schedule__", {"type": "interval", "seconds": seconds, "name": name})
        return handler

    return decorator


def Cron(expression: str, name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_schedule__", {"type": "cron", "expression": expression, "name": name})
        return handler

    return decorator


def CronJob(
    expression: str,
    *,
    name: str | None = None,
    time_zone: str | None = None,
    utc_offset: int | None = None,
    disabled: bool = False,
    wait_for_completion: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(
            handler,
            "__fanest_schedule__",
            {
                "type": "cron",
                "expression": expression,
                "name": name,
                "time_zone": time_zone,
                "utc_offset": utc_offset,
                "disabled": disabled,
                "wait_for_completion": wait_for_completion,
            },
        )
        return handler

    return decorator


def Timeout(seconds: float, name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_schedule__", {"type": "timeout", "seconds": seconds, "name": name})
        return handler

    return decorator
