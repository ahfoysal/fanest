import time
from typing import Any, Callable, Protocol
from uuid import uuid4

from fanest import Injectable, Module, Optional, use_value
from fanest.common.exceptions import FaNestHttpException
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

THROTTLER_OPTIONS = token("THROTTLER_OPTIONS")


class ThrottlerStore(Protocol):
    def hit(self, key: str, *, limit: int, ttl: int) -> bool: ...


class MemoryThrottlerStore:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def hit(self, key: str, *, limit: int, ttl: int) -> bool:
        now = time.monotonic()
        window_start = now - ttl
        hits = [hit for hit in self._hits.get(key, []) if hit >= window_start]
        if len(hits) >= limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


class RedisThrottlerStore:
    def __init__(self, *, url: str = "redis://localhost:6379/0", prefix: str = "fanest:throttle:") -> None:
        try:
            import redis  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover - exercised without redis installed
            raise ImportError(
                "RedisThrottlerStore requires the 'redis' package. "
                "Install it with: pip install 'fanest[redis]'"
            ) from exc
        self.prefix = prefix
        self._client = redis.Redis.from_url(url)

    def hit(self, key: str, *, limit: int, ttl: int) -> bool:
        redis_key = f"{self.prefix}{key}"
        now = time.time()
        window_start = now - ttl
        member = f"{now}:{uuid4()}"
        pipe = self._client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zcard(redis_key)
        pipe.zadd(redis_key, {member: now})
        pipe.expire(redis_key, ttl)
        _, count, *_ = pipe.execute()
        return int(count) < limit


@Injectable()
class ThrottlerService:
    _limit = 10
    _ttl = 60

    def __init__(self, options: dict[str, Any] | None = Optional(THROTTLER_OPTIONS)):
        options = options or {"limit": self._limit, "ttl": self._ttl}
        self.limit = options.get("limit", self._limit)
        self.ttl = options.get("ttl", self._ttl)
        store = options.get("store")
        if store is not None:
            self.store: ThrottlerStore = store
        elif options.get("redis_url"):
            self.store = RedisThrottlerStore(
                url=options["redis_url"],
                prefix=options.get("redis_prefix", "fanest:throttle:"),
            )
        else:
            self.store = MemoryThrottlerStore()

    @classmethod
    def configure(cls, *, limit: int, ttl: int) -> None:
        cls._limit = limit
        cls._ttl = ttl

    def hit(self, key: str, *, limit: int | None = None, ttl: int | None = None) -> bool:
        resolved_limit = limit if limit is not None else self.limit
        resolved_ttl = ttl if ttl is not None else self.ttl
        return self.store.hit(key, limit=resolved_limit, ttl=resolved_ttl)


class ThrottlerGuard:
    def __init__(self, throttler_service: ThrottlerService):
        self.throttler_service = throttler_service

    def can_activate(self, context):
        options = getattr(context.handler, "__fanest_throttle__", {})
        tracker = context.request.client.host if context.request.client else "anonymous"
        route = getattr(context.handler, "__qualname__", repr(context.handler))
        method = getattr(context.request, "method", "")
        path_template = getattr(getattr(context.request, "scope", {}), "get", lambda *_: None)("route")
        route_path = getattr(path_template, "path", None) or getattr(getattr(context.request, "url", None), "path", "")
        key = f"{tracker}:{method}:{route_path}:{route}"
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
    def for_root(
        *,
        limit: int = 10,
        ttl: int = 60,
        is_global: bool = False,
        store: ThrottlerStore | None = None,
        redis_url: str | None = None,
        redis_prefix: str = "fanest:throttle:",
    ) -> type:
        ThrottlerService.configure(limit=limit, ttl=ttl)
        options = {
            "limit": limit,
            "ttl": ttl,
            "store": store,
            "redis_url": redis_url,
            "redis_prefix": redis_prefix,
        }

        @Module(
            providers=[use_value(THROTTLER_OPTIONS, options), ThrottlerService, ThrottlerGuard],
            exports=[ThrottlerService, ThrottlerGuard],
            global_module=is_global,
        )
        class DynamicThrottlerModule:
            pass

        return DynamicThrottlerModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any]],
        inject: list[Any] | None = None,
        is_global: bool = False,
    ) -> type:
        @Module(
            providers=[
                provider_factory(THROTTLER_OPTIONS, use_factory, inject=inject or []),
                ThrottlerService,
                ThrottlerGuard,
            ],
            exports=[ThrottlerService, ThrottlerGuard],
            global_module=is_global,
        )
        class DynamicThrottlerModule:
            pass

        return DynamicThrottlerModule
