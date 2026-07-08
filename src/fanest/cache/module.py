import json
import time
from typing import Any, Protocol

from fanest import Injectable, Module, Optional, use_value
from fanest.core.providers import token

CACHE_OPTIONS = token("CACHE_OPTIONS")


class CacheStore(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl: int | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def clear(self) -> None: ...


class MemoryCacheStore:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float | None, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at is not None and expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        expires_at = time.monotonic() + ttl if ttl is not None else None
        self._store[key] = (expires_at, value)

    def clear(self) -> None:
        self._store.clear()

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


class RedisCacheStore:
    """A real Redis-backed cache store (requires the ``redis`` package).

    Values are JSON-serialized. Keys are namespaced by ``prefix`` so ``clear()``
    only removes this cache's entries (never a blind ``FLUSHDB``).
    """

    def __init__(self, *, url: str = "redis://localhost:6379/0", prefix: str = "fanest:cache:") -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - exercised without redis installed
            raise ImportError(
                "RedisCacheStore requires the 'redis' package. "
                "Install it with: pip install 'fanest[redis]'"
            ) from exc
        self.url = url
        self.prefix = prefix
        self._client = redis.Redis.from_url(url)

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get(self, key: str) -> Any | None:
        raw = self._client.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        data = json.dumps(value)
        self._client.set(self._key(key), data, ex=int(ttl) if ttl else None)

    def delete(self, key: str) -> None:
        self._client.delete(self._key(key))

    def clear(self) -> None:
        for key in self._client.scan_iter(match=f"{self.prefix}*"):
            self._client.delete(key)


@Injectable()
class CacheService:
    def __init__(self, options: dict[str, Any] | None = Optional(CACHE_OPTIONS)):
        options = options or {}
        store = options.get("store")
        if store is not None:
            self.store: CacheStore = store
        elif options.get("redis_url"):
            self.store = RedisCacheStore(url=options["redis_url"])
        else:
            self.store = MemoryCacheStore()

    def get(self, key: str) -> Any | None:
        return self.store.get(key)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.store.set(key, value, ttl)

    def clear(self) -> None:
        self.store.clear()

    def delete(self, key: str) -> None:
        self.store.delete(key)


class CacheInterceptor:
    def __init__(self, cache_service: CacheService):
        self.cache_service = cache_service

    async def intercept(self, context, call_next):
        request = context.request
        evict_key = getattr(context.handler, "__fanest_cache_evict__", None)
        if evict_key is not None:
            self.cache_service.delete(evict_key)
            return await call_next()
        if request.method != "GET":
            return await call_next()
        key = self._cache_key(context)
        cached = self.cache_service.get(key)
        if cached is not None:
            return cached
        result = await call_next()
        ttl = getattr(context.handler, "__fanest_cache_ttl__", None)
        self.cache_service.set(key, result, ttl)
        return result

    def _cache_key(self, context) -> str:
        custom_key = getattr(context.handler, "__fanest_cache_key__", None)
        if custom_key is not None:
            return custom_key
        request = context.request
        return f"{request.method}:{request.url.path}?{request.url.query}"


def CacheTTL(seconds: int):
    def decorator(handler):
        setattr(handler, "__fanest_cache_ttl__", seconds)
        return handler

    return decorator


def CacheKey(key: str):
    def decorator(handler):
        setattr(handler, "__fanest_cache_key__", key)
        return handler

    return decorator


def CacheEvict(key: str):
    def decorator(handler):
        setattr(handler, "__fanest_cache_evict__", key)
        return handler

    return decorator


class CacheModule:
    @staticmethod
    def register(is_global: bool = False, **options: Any) -> type:
        @Module(
            providers=[use_value(CACHE_OPTIONS, options), CacheService, CacheInterceptor],
            exports=[CacheService, CacheInterceptor],
            global_module=is_global,
        )
        class DynamicCacheModule:
            pass

        return DynamicCacheModule
