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


class CacheInterceptor:
    def __init__(self, cache_service: CacheService):
        self.cache_service = cache_service

    async def intercept(self, context, call_next):
        request = context.request
        key = f"{request.method}:{request.url.path}?{request.url.query}"
        cached = self.cache_service.get(key)
        if cached is not None:
            return cached
        result = await call_next()
        ttl = getattr(context.handler, "__fanest_cache_ttl__", None)
        self.cache_service.set(key, result, ttl)
        return result


def CacheTTL(seconds: int):
    def decorator(handler):
        setattr(handler, "__fanest_cache_ttl__", seconds)
        return handler

    return decorator


class CacheModule:
    @staticmethod
    def register() -> type:
        @Module(providers=[CacheService, CacheInterceptor], exports=[CacheService, CacheInterceptor])
        class DynamicCacheModule:
            pass

        return DynamicCacheModule
