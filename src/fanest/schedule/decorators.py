from collections.abc import Callable
from typing import Any


def Interval(seconds: float) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_schedule__", {"type": "interval", "seconds": seconds})
        return handler

    return decorator


def Cron(expression: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_schedule__", {"type": "cron", "expression": expression})
        return handler

    return decorator
