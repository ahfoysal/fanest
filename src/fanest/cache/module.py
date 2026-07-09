import copy
import hashlib
import inspect
import json
import time
from typing import Any, Awaitable, Callable, Protocol

from fanest import Injectable, Module, Optional, use_value
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

CACHE_OPTIONS = token("CACHE_OPTIONS")


class CacheStore(Protocol):
    def get(self, key: str) -> Any | None | Awaitable[Any | None]: ...

    def set(self, key: str, value: Any, ttl: int | None = None) -> None | Awaitable[None]: ...

    def delete(self, key: str) -> None | Awaitable[None]: ...

    def clear(self) -> None | Awaitable[None]: ...


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
        return copy.deepcopy(value)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        expires_at = time.monotonic() + ttl if ttl is not None else None
        self._store[key] = (expires_at, copy.deepcopy(value))

    def clear(self) -> None:
        self._store.clear()

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self._store if k == prefix or k.startswith(f"{prefix}|")]:
            self._store.pop(key, None)


class RedisCacheStore:
    """A real Redis-backed cache store (requires the ``redis`` package).

    Values are JSON-serialized. Keys are namespaced by ``prefix`` so ``clear()``
    only removes this cache's entries (never a blind ``FLUSHDB``).
    """

    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379/0",
        prefix: str = "fanest:cache:",
        client: Any | None = None,
    ) -> None:
        self.url = url
        self.prefix = prefix
        if client is not None:
            self._client = client
            return
        try:
            import redis  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover - exercised without redis installed
            raise ImportError(
                "RedisCacheStore requires the 'redis' package. "
                "Install it with: pip install 'fanest[redis]'"
            ) from exc
        self._client = redis.Redis.from_url(url)

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get(self, key: str) -> Any | None:
        raw = self._client.get(self._key(key))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        data = json.dumps(value)
        if ttl is not None and ttl <= 0:
            self.delete(key)
            return
        self._client.set(self._key(key), data, ex=int(ttl) if ttl is not None else None)

    def delete(self, key: str) -> None:
        self._client.delete(self._key(key))

    def delete_prefix(self, prefix: str) -> None:
        full = self._key(prefix)
        matched = []
        for raw in self._client.scan_iter(match=f"{full}*"):
            candidate = raw.decode() if isinstance(raw, bytes) else raw
            if candidate == full or candidate.startswith(f"{full}|"):
                matched.append(raw)
        if matched:
            self._client.delete(*matched)

    def clear(self) -> None:
        keys = list(self._client.scan_iter(match=f"{self.prefix}*"))
        if keys:
            self._client.delete(*keys)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


@Injectable()
class CacheService:
    def __init__(self, options: dict[str, Any] | None = Optional(CACHE_OPTIONS)):
        options = options or {}
        self.default_ttl: int | None = options.get("ttl", 60)
        store = options.get("store")
        if store is not None:
            self.store: CacheStore = store
        elif options.get("redis_url") or options.get("redis_client") is not None:
            self.store = RedisCacheStore(
                url=options.get("redis_url", "redis://localhost:6379/0"),
                prefix=options.get("redis_prefix", "fanest:cache:"),
                client=options.get("redis_client"),
            )
        else:
            self.store = MemoryCacheStore()

    def get(self, key: str) -> Any | None:
        return self.store.get(key)

    async def get_async(self, key: str) -> Any | None:
        result = self.store.get(key)
        if inspect.isawaitable(result):
            return await result
        return result

    def mget(self, *keys: str) -> list[Any | None]:
        return [self.get(key) for key in keys]

    async def mget_async(self, *keys: str) -> list[Any | None]:
        return [await self.get_async(key) for key in keys]

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.store.set(key, value, self.default_ttl if ttl is None else ttl)

    async def set_async(self, key: str, value: Any, ttl: int | None = None) -> None:
        result = self.store.set(key, value, self.default_ttl if ttl is None else ttl)
        if inspect.isawaitable(result):
            await result

    def mset(self, values: dict[str, Any] | list[tuple[str, Any]], ttl: int | None = None) -> None:
        items = values.items() if isinstance(values, dict) else values
        for key, value in items:
            self.set(key, value, ttl)

    async def mset_async(self, values: dict[str, Any] | list[tuple[str, Any]], ttl: int | None = None) -> None:
        items = values.items() if isinstance(values, dict) else values
        for key, value in items:
            await self.set_async(key, value, ttl)

    async def remember(self, key: str, factory: Callable[[], Any], ttl: int | None = None) -> Any:
        cached = await self.get_async(key)
        if cached is not None:
            return cached
        value = factory()
        if inspect.isawaitable(value):
            value = await value
        await self.set_async(key, value, ttl)
        return value

    def wrap(self, key: str, factory: Callable[[], Any], ttl: int | None = None) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = factory()
        if inspect.isawaitable(value):
            raise RuntimeError("Async cache factories require wrap_async().")
        self.set(key, value, ttl)
        return value

    async def wrap_async(self, key: str, factory: Callable[[], Any], ttl: int | None = None) -> Any:
        return await self.remember(key, factory, ttl)

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    async def has_async(self, key: str) -> bool:
        return await self.get_async(key) is not None

    def clear(self) -> None:
        self.store.clear()

    reset = clear

    async def clear_async(self) -> None:
        result = self.store.clear()
        if inspect.isawaitable(result):
            await result

    async def reset_async(self) -> None:
        await self.clear_async()

    def delete(self, key: str) -> None:
        self.store.delete(key)

    del_ = delete

    async def delete_async(self, key: str) -> None:
        result = self.store.delete(key)
        if inspect.isawaitable(result):
            await result

    async def del_async(self, key: str) -> None:
        await self.delete_async(key)

    def delete_prefix(self, prefix: str) -> None:
        delete_prefix = getattr(self.store, "delete_prefix", None)
        if delete_prefix is not None:
            result = delete_prefix(prefix)
            if inspect.isawaitable(result):
                raise RuntimeError("Async cache stores require delete_prefix_async().")
            return
        self.delete(prefix)

    async def delete_prefix_async(self, prefix: str) -> None:
        delete_prefix = getattr(self.store, "delete_prefix", None)
        if delete_prefix is not None:
            result = delete_prefix(prefix)
            if inspect.isawaitable(result):
                await result
            return
        await self.delete_async(prefix)

    def mdelete(self, *keys: str) -> None:
        for key in keys:
            self.delete(key)

    mdel = mdelete

    async def mdelete_async(self, *keys: str) -> None:
        for key in keys:
            await self.delete_async(key)

    mdel_async = mdelete_async

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if close is not None:
            close()

    async def close_async(self) -> None:
        close = getattr(self.store, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result


class CacheInterceptor:
    def __init__(self, cache_service: CacheService):
        self.cache_service = cache_service

    async def intercept(self, context, call_next):
        request = context.request
        evict_key = getattr(context.handler, "__fanest_cache_evict__", None)
        if evict_key is not None:
            # Stored GET responses use composite keys (``evict_key|query:...``,
            # ``evict_key|identity:...``); evict by prefix so authenticated and
            # query-param variants are invalidated too, not only the bare key.
            await self.cache_service.delete_prefix_async(evict_key)
            return await call_next()
        if request.method != "GET":
            return await call_next()
        key = self._cache_key(context)
        cached = await self.cache_service.get_async(key)
        if cached is not None:
            return cached
        result = await call_next()
        ttl = getattr(context.handler, "__fanest_cache_ttl__", None)
        if ttl is None:
            ttl = self.cache_service.default_ttl
        await self.cache_service.set_async(key, result, ttl)
        return result

    def _cache_key(self, context) -> str:
        custom_key = getattr(context.handler, "__fanest_cache_key__", None)
        request = context.request
        identity = self._identity_fragment(request)
        if custom_key is not None:
            parts = [custom_key]
            if request.url.query:
                parts.append(f"query:{request.url.query}")
            if identity:
                parts.append(f"identity:{identity}")
            return "|".join(parts)
        parts = [request.method, request.url.path, request.url.query]
        if identity:
            parts.append(identity)
        return ":".join(parts)

    def _identity_fragment(self, request) -> str | None:
        authorization = request.headers.get("authorization")
        cookie = request.headers.get("cookie")
        if authorization is None and cookie is None:
            return None
        raw = f"{authorization or ''}\0{cookie or ''}".encode()
        return hashlib.sha256(raw).hexdigest()[:16]


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

    for_root = register

    @staticmethod
    def register_async(
        *,
        use_factory: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]],
        inject: list[Any] | None = None,
        is_global: bool = False,
    ) -> type:
        @Module(
            providers=[
                provider_factory(CACHE_OPTIONS, use_factory, inject=inject or []),
                CacheService,
                CacheInterceptor,
            ],
            exports=[CacheService, CacheInterceptor],
            global_module=is_global,
        )
        class DynamicCacheModule:
            pass

        return DynamicCacheModule

    for_root_async = register_async
