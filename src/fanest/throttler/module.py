import time

from fanest import Injectable, Module
from fanest.common.exceptions import FaNestHttpException


@Injectable()
class ThrottlerService:
    _limit = 10
    _ttl = 60
    _hits: dict[str, list[float]] = {}

    @classmethod
    def configure(cls, *, limit: int, ttl: int) -> None:
        cls._limit = limit
        cls._ttl = ttl

    def hit(self, key: str, *, limit: int | None = None, ttl: int | None = None) -> bool:
        limit = limit or self._limit
        ttl = ttl or self._ttl
        now = time.monotonic()
        window_start = now - ttl
        hits = [hit for hit in self._hits.get(key, []) if hit >= window_start]
        if len(hits) >= limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


class ThrottlerGuard:
    def __init__(self, throttler_service: ThrottlerService):
        self.throttler_service = throttler_service

    def can_activate(self, context):
        options = getattr(context.handler, "__fanest_throttle__", {})
        key = context.request.client.host if context.request.client else "anonymous"
        if self.throttler_service.hit(
            key,
            limit=options.get("limit"),
            ttl=options.get("ttl"),
        ):
            return True
        raise FaNestHttpException(429, "Too Many Requests")


def Throttle(*, limit: int | None = None, ttl: int | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_throttle__", {"limit": limit, "ttl": ttl})
        return handler

    return decorator


class ThrottlerModule:
    @staticmethod
    def for_root(*, limit: int = 10, ttl: int = 60) -> type:
        ThrottlerService.configure(limit=limit, ttl=ttl)

        @Module(providers=[ThrottlerService, ThrottlerGuard], exports=[ThrottlerService, ThrottlerGuard])
        class DynamicThrottlerModule:
            pass

        return DynamicThrottlerModule
