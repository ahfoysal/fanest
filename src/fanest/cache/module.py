import time
from typing import Any

from fanest import Injectable, Module


@Injectable()
class CacheService:
    _store: dict[str, tuple[float | None, Any]] = {}

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
    def register() -> type:
        @Module(providers=[CacheService, CacheInterceptor], exports=[CacheService, CacheInterceptor])
        class DynamicCacheModule:
            pass

        return DynamicCacheModule
