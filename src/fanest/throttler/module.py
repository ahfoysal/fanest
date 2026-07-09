import inspect
import time
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

from fanest import Injectable, Module, Optional, use_value
from fanest.common.exceptions import FaNestHttpException
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

THROTTLER_OPTIONS = token("THROTTLER_OPTIONS")


class ThrottlerStore(Protocol):
    def hit(self, key: str, *, limit: int, ttl: int) -> bool | Awaitable[bool]: ...


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
    _HIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_start = tonumber(ARGV[2])
local member = ARGV[3]
local limit = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
local count = redis.call('ZCARD', key)
if count >= limit then
    redis.call('EXPIRE', key, ttl)
    return 0
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, ttl)
return 1
"""

    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379/0",
        prefix: str = "fanest:throttle:",
        client: Any | None = None,
    ) -> None:
        self.prefix = prefix
        if client is not None:
            self._client = client
            return
        try:
            import redis  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover - exercised without redis installed
            raise ImportError(
                "RedisThrottlerStore requires the 'redis' package. "
                "Install it with: pip install 'fanest[redis]'"
            ) from exc
        self._client = redis.Redis.from_url(url)

    def hit(self, key: str, *, limit: int, ttl: int) -> bool:
        if limit <= 0:
            return False
        if ttl <= 0:
            return True
        redis_key = f"{self.prefix}{key}"
        now = time.time()
        window_start = now - ttl
        member = f"{now}:{uuid4()}"
        eval_command = getattr(self._client, "eval", None)
        if eval_command is not None:
            result = eval_command(
                self._HIT_SCRIPT,
                1,
                redis_key,
                now,
                window_start,
                member,
                limit,
                ttl,
            )
            return bool(int(result))
        pipe = self._client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zcard(redis_key)
        _, count = pipe.execute()
        if int(count) >= limit:
            self._expire(redis_key, ttl)
            return False
        zadd = getattr(self._client, "zadd", None)
        if zadd is not None:
            zadd(redis_key, {member: now})
            self._expire(redis_key, ttl)
            return True
        pipe = self._client.pipeline()
        pipe.zadd(redis_key, {member: now})
        pipe.expire(redis_key, ttl)
        pipe.execute()
        return True

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    def _expire(self, key: str, ttl: int) -> None:
        expire = getattr(self._client, "expire", None)
        if expire is not None:
            expire(key, ttl)
            return
        pipe = self._client.pipeline()
        pipe.expire(key, ttl)
        pipe.execute()


@Injectable()
class ThrottlerService:
    _limit = 10
    _ttl = 60

    def __init__(self, options: dict[str, Any] | None = Optional(THROTTLER_OPTIONS)):
        options = options or {"limit": self._limit, "ttl": self._ttl}
        self.limit = options.get("limit", self._limit)
        self.ttl = options.get("ttl", self._ttl)
        self.get_tracker = options.get("get_tracker")
        self.generate_key = options.get("generate_key")
        store = options.get("store")
        if store is not None:
            self.store: ThrottlerStore = store
        elif options.get("redis_url") or options.get("redis_client") is not None:
            self.store = RedisThrottlerStore(
                url=options.get("redis_url", "redis://localhost:6379/0"),
                prefix=options.get("redis_prefix", "fanest:throttle:"),
                client=options.get("redis_client"),
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
        result = self.store.hit(key, limit=resolved_limit, ttl=resolved_ttl)
        if inspect.isawaitable(result):
            raise RuntimeError("Async throttler stores require hit_async().")
        return result

    async def hit_async(self, key: str, *, limit: int | None = None, ttl: int | None = None) -> bool:
        resolved_limit = limit if limit is not None else self.limit
        resolved_ttl = ttl if ttl is not None else self.ttl
        result: Any = self.store.hit(key, limit=resolved_limit, ttl=resolved_ttl)
        if inspect.isawaitable(result):
            return await result
        return result

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if close is not None:
            close()


class ThrottlerGuard:
    def __init__(self, throttler_service: ThrottlerService):
        self.throttler_service = throttler_service

    async def can_activate(self, context):
        if getattr(context.controller, "__fanest_skip_throttle__", False) or getattr(
            context.handler,
            "__fanest_skip_throttle__",
            False,
        ):
            return True
        options = getattr(context.handler, "__fanest_throttle__", {})
        tracker = await self._tracker(context)
        route = getattr(context.handler, "__qualname__", repr(context.handler))
        method = getattr(context.request, "method", "")
        path_template = getattr(getattr(context.request, "scope", {}), "get", lambda *_: None)("route")
        route_path = getattr(path_template, "path", None) or getattr(getattr(context.request, "url", None), "path", "")
        key = await self._key(context, tracker, method, route_path, route)
        if await self.throttler_service.hit_async(
            key,
            limit=options.get("limit"),
            ttl=options.get("ttl"),
        ):
            return True
        raise FaNestHttpException(429, "Too Many Requests")

    async def _tracker(self, context) -> str:
        if self.throttler_service.get_tracker is not None:
            result = self.throttler_service.get_tracker(context)
            if inspect.isawaitable(result):
                result = await result
            return str(result)
        return context.request.client.host if context.request.client else "anonymous"

    async def _key(self, context, tracker: str, method: str, route_path: str, route: str) -> str:
        if self.throttler_service.generate_key is not None:
            result = self.throttler_service.generate_key(context, tracker, method, route_path, route)
            if inspect.isawaitable(result):
                result = await result
            return str(result)
        return f"{tracker}:{method}:{route_path}:{route}"


def Throttle(*, limit: int | None = None, ttl: int | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_throttle__", {"limit": limit, "ttl": ttl})
        return handler

    return decorator


def SkipThrottle(skip: bool = True):
    def decorator(target):
        setattr(target, "__fanest_skip_throttle__", skip)
        return target

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
        redis_client: Any | None = None,
        get_tracker: Callable[[Any], Any] | None = None,
        generate_key: Callable[[Any, str, str, str, str], Any] | None = None,
    ) -> type:
        ThrottlerService.configure(limit=limit, ttl=ttl)
        options = {
            "limit": limit,
            "ttl": ttl,
            "store": store,
            "redis_url": redis_url,
            "redis_prefix": redis_prefix,
            "redis_client": redis_client,
            "get_tracker": get_tracker,
            "generate_key": generate_key,
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
        use_factory: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]],
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
