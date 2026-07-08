from collections.abc import Callable
from typing import Any


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


def Timeout(seconds: float, name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        setattr(handler, "__fanest_schedule__", {"type": "timeout", "seconds": seconds, "name": name})
        return handler

    return decorator
