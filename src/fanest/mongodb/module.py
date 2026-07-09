from copy import deepcopy
import inspect
import re
from typing import Any, Awaitable, Callable, cast
from uuid import uuid4

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token, use_factory as provider_factory

MONGO_OPTIONS = token("MONGO_OPTIONS")
_root_module_cache: dict[Any, type] = {}
_async_root_module_cache: dict[tuple[int, tuple[Any, ...], bool], type] = {}
_feature_module_cache: dict[tuple[str, ...], type] = {}


def collection_token(name: str):
    return token(f"MONGO_COLLECTION:{name}")


def InjectModel(name: str):
    return Inject(collection_token(name))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze(item) for item in value))
    try:
        hash(value)
    except TypeError:
        return (type(value), id(value))
    return value


class MongoCollection:
    def __init__(self, name: str):
        self.name = name
        self._documents: dict[str, dict[str, Any]] = {}

    async def insert_one(self, document: dict[str, Any]) -> dict[str, Any]:
        stored = deepcopy(document)
        stored.setdefault("_id", str(uuid4()))
        self._documents[str(stored["_id"])] = stored
        return deepcopy(stored)

    async def insert_many(self, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [await self.insert_one(document) for document in documents]

    async def find(
        self,
        query: dict[str, Any] | None = None,
        *,
        sort: str | tuple[str, int | str] | list[tuple[str, int | str]] | None = None,
        skip: int = 0,
        limit: int | None = None,
        projection: list[str] | dict[str, int | bool] | None = None,
    ) -> list[dict[str, Any]]:
        query = query or {}
        documents = [deepcopy(document) for document in self._documents.values() if self._matches(document, query)]
        if sort is not None:
            documents = _sort_documents(documents, sort)
        if skip:
            documents = documents[skip:]
        if limit is not None:
            documents = documents[:limit]
        if projection is not None:
            documents = [_project_document(document, projection) for document in documents]
        return documents

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for document in await self.find(query):
            return document
        return None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> dict[str, Any] | None:
        match = await self.find_one(query)
        if match is None:
            return None
        stored = self._documents[str(match["_id"])]
        _apply_update(stored, update)
        return deepcopy(stored)

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> int:
        matched = 0
        for key, document in list(self._documents.items()):
            if self._matches(document, query):
                _apply_update(self._documents[key], update)
                matched += 1
        return matched

    async def delete_one(self, query: dict[str, Any]) -> bool:
        match = await self.find_one(query)
        if match is None:
            return False
        self._documents.pop(str(match["_id"]), None)
        return True

    async def delete_many(self, query: dict[str, Any]) -> int:
        deleted = 0
        for key, document in list(self._documents.items()):
            if self._matches(document, query):
                self._documents.pop(key, None)
                deleted += 1
        return deleted

    async def count_documents(self, query: dict[str, Any] | None = None) -> int:
        return len(await self.find(query or {}))

    async def distinct(self, field: str, query: dict[str, Any] | None = None) -> list[Any]:
        values = []
        for document in await self.find(query or {}):
            value = _get_path(document, field, _MISSING)
            if value is _MISSING:
                continue
            if isinstance(value, list):
                for item in value:
                    if item not in values:
                        values.append(item)
            elif value not in values:
                values.append(value)
        return values

    def clear(self) -> None:
        self._documents.clear()

    def _matches(self, document: dict[str, Any], query: dict[str, Any]) -> bool:
        return _matches_query(document, query)


def _get_path(document: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _set_path(document: dict[str, Any], path: str, value: Any) -> None:
    current = document
    parts = path.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _unset_path(document: dict[str, Any], path: str) -> None:
    current = document
    parts = path.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            return
        current = next_value
    current.pop(parts[-1], None)


def _apply_update(stored: dict[str, Any], update: dict[str, Any]) -> None:
    if any(str(key).startswith("$") for key in update):
        allowed = {"$set", "$unset", "$inc", "$push", "$pull", "$addToSet"}
        unsupported = set(update) - allowed
        if unsupported:
            raise ValueError(f"Unsupported Mongo update operator(s): {', '.join(sorted(unsupported))}")
        for key, value in update.get("$set", {}).items():
            _set_path(stored, key, value)
        for key in update.get("$unset", {}):
            _unset_path(stored, key)
        for key, value in update.get("$inc", {}).items():
            current = _get_path(stored, key, 0)
            if not isinstance(current, int | float) or not isinstance(value, int | float):
                raise TypeError(f"Cannot apply $inc to non-numeric field: {key}")
            _set_path(stored, key, current + value)
        for key, value in update.get("$push", {}).items():
            current = _get_path(stored, key, [])
            if not isinstance(current, list):
                raise TypeError(f"Cannot apply $push to non-array field: {key}")
            current.append(value)
            _set_path(stored, key, current)
        for key, value in update.get("$pull", {}).items():
            current = _get_path(stored, key, [])
            if not isinstance(current, list):
                raise TypeError(f"Cannot apply $pull to non-array field: {key}")
            _set_path(stored, key, [item for item in current if item != value])
        for key, value in update.get("$addToSet", {}).items():
            current = _get_path(stored, key, [])
            if not isinstance(current, list):
                raise TypeError(f"Cannot apply $addToSet to non-array field: {key}")
            if value not in current:
                current.append(value)
            _set_path(stored, key, current)
        return
    for key, value in update.items():
        _set_path(stored, key, value)


def _sort_documents(
    documents: list[dict[str, Any]],
    sort: str | tuple[str, int | str] | list[tuple[str, int | str]],
) -> list[dict[str, Any]]:
    sort_fields: list[tuple[str, int | str]]
    if isinstance(sort, str):
        sort_fields = [(sort, 1)]
    elif isinstance(sort, tuple):
        sort_fields = [sort]
    else:
        sort_fields = sort
    ordered = documents
    for field, direction in reversed(sort_fields):
        reverse = direction in {-1, "desc", "DESC", "descending"}
        ordered = sorted(ordered, key=lambda item, field=field: _get_path(item, field), reverse=reverse)
    return ordered


def _project_document(document: dict[str, Any], projection: list[str] | dict[str, int | bool]) -> dict[str, Any]:
    if isinstance(projection, list):
        fields = {field for field in projection}
        include_id = "_id" not in fields
    else:
        included = {field for field, enabled in projection.items() if enabled}
        excluded = {field for field, enabled in projection.items() if not enabled}
        if included:
            fields = included
            include_id = "_id" not in excluded
        else:
            projected = deepcopy(document)
            for field in excluded:
                _unset_path(projected, field)
            return projected
    projected: dict[str, Any] = {}
    if include_id and "_id" in document:
        projected["_id"] = document["_id"]
    for field in fields:
        value = _get_path(document, field, _MISSING)
        if value is not _MISSING:
            _set_path(projected, field, deepcopy(value))
    return projected


def _matches_query(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches_query(document, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches_query(document, item) for item in expected):
                return False
            continue
        if key == "$nor":
            if any(_matches_query(document, item) for item in expected):
                return False
            continue
        actual = _get_path(document, key)
        exists = _get_path(document, key, _MISSING) is not _MISSING
        if isinstance(expected, dict) and any(str(operator).startswith("$") for operator in expected):
            if not _matches_operators(actual, exists, expected):
                return False
        elif isinstance(actual, list):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


_MISSING = object()


def _matches_operators(actual: Any, exists: bool, operators: dict[str, Any]) -> bool:
    for operator, expected in operators.items():
        if operator == "$eq" and actual != expected:
            return False
        if operator == "$ne" and actual == expected:
            return False
        if operator == "$gt" and (not exists or actual <= expected):
            return False
        if operator == "$gte" and (not exists or actual < expected):
            return False
        if operator == "$lt" and (not exists or actual >= expected):
            return False
        if operator == "$lte" and (not exists or actual > expected):
            return False
        if operator == "$in" and not _value_in(actual, expected):
            return False
        if operator == "$nin" and _value_in(actual, expected):
            return False
        if operator == "$exists" and exists is not bool(expected):
            return False
        if operator == "$regex" and not re.search(str(expected), str(actual or "")):
            return False
        if operator not in {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists", "$regex"}:
            raise ValueError(f"Unsupported Mongo query operator: {operator}")
    return True


def _value_in(actual: Any, expected_values: Any) -> bool:
    if isinstance(actual, list):
        return any(item in expected_values for item in actual)
    return actual in expected_values


class MotorCollection:
    """Real MongoDB-backed collection (requires ``motor``), matching the
    in-memory :class:`MongoCollection` contract: string ``_id`` values that
    round-trip, and the same insert/find/update/delete methods.
    """

    def __init__(self, motor_collection: Any):
        self._collection = motor_collection

    async def insert_one(self, document: dict[str, Any]) -> dict[str, Any]:
        stored = dict(document)
        stored.setdefault("_id", str(uuid4()))
        await self._collection.insert_one(stored)
        return dict(stored)

    async def insert_many(self, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stored = [dict(document, _id=document.get("_id", str(uuid4()))) for document in documents]
        if stored:
            await self._collection.insert_many(stored)
        return stored

    async def find(
        self,
        query: dict[str, Any] | None = None,
        *,
        sort: str | tuple[str, int | str] | list[tuple[str, int | str]] | None = None,
        skip: int = 0,
        limit: int | None = None,
        projection: list[str] | dict[str, int | bool] | None = None,
    ) -> list[dict[str, Any]]:
        cursor = self._collection.find(query or {}, projection=projection)
        if sort is not None:
            if isinstance(sort, str):
                cursor = cursor.sort(sort, 1)
            else:
                cursor = cursor.sort([sort] if isinstance(sort, tuple) else sort)
        if skip:
            cursor = cursor.skip(skip)
        if limit is not None:
            cursor = cursor.limit(limit)
        return [document async for document in cursor]

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return await self._collection.find_one(query)

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> dict[str, Any] | None:
        changes = update if any(str(k).startswith("$") for k in update) else {"$set": update}
        await self._collection.update_one(query, changes)
        return await self.find_one(query)

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> int:
        changes = update if any(str(k).startswith("$") for k in update) else {"$set": update}
        result = await self._collection.update_many(query, changes)
        return int(result.modified_count)

    async def delete_one(self, query: dict[str, Any]) -> bool:
        result = await self._collection.delete_one(query)
        return result.deleted_count > 0

    async def delete_many(self, query: dict[str, Any]) -> int:
        result = await self._collection.delete_many(query)
        return int(result.deleted_count)

    async def count_documents(self, query: dict[str, Any] | None = None) -> int:
        return int(await self._collection.count_documents(query or {}))

    async def distinct(self, field: str, query: dict[str, Any] | None = None) -> list[Any]:
        return list(await self._collection.distinct(field, query or {}))

    async def clear(self) -> None:
        await self._collection.delete_many({})


@Injectable()
class MongoService:
    def __init__(self, options: dict[str, Any] = Inject(MONGO_OPTIONS)):
        self.options = options
        self._collections: dict[str, Any] = {}
        self._client = options.get("client")
        self._db = options.get("db")
        self._closed = False
        uri = options.get("uri") or options.get("url")
        if self._db is None and uri:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover - exercised without motor installed
                raise ImportError(
                    "MongoModule.for_root(uri=...) requires the 'motor' package. "
                    "Install it with: pip install 'fanest[mongo]'"
                ) from exc
            self._client = self._client or AsyncIOMotorClient(uri)
            self._db = self._client[options.get("database", "fanest")]

    def collection(self, name: str) -> Any:
        if self._closed:
            raise RuntimeError("MongoService has been closed.")
        if name not in self._collections:
            if self._db is not None:
                self._collections[name] = MotorCollection(self._db[name])
            else:
                self._collections[name] = MongoCollection(name)
        return self._collections[name]

    async def on_application_shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            result = self._client.close()
            if inspect.isawaitable(result):
                await cast(Awaitable[Any], result)


class MongoModule:
    @staticmethod
    def for_root(is_global: bool = True, **options: Any) -> type:
        cache_key = _freeze({**options, "is_global": is_global})
        if cache_key in _root_module_cache:
            return _root_module_cache[cache_key]

        @Module(
            providers=[use_value(MONGO_OPTIONS, options), MongoService],
            exports=[MongoService],
            global_module=is_global,
        )
        class DynamicMongoModule:
            pass

        _root_module_cache[cache_key] = DynamicMongoModule
        return DynamicMongoModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]],
        inject: list[Any] | None = None,
        is_global: bool = True,
    ) -> type:
        cache_key = (id(use_factory), tuple(inject or []), is_global)
        if cache_key in _async_root_module_cache:
            return _async_root_module_cache[cache_key]

        @Module(
            providers=[provider_factory(MONGO_OPTIONS, use_factory, inject=inject or []), MongoService],
            exports=[MongoService],
            global_module=is_global,
        )
        class DynamicMongoModule:
            pass

        _async_root_module_cache[cache_key] = DynamicMongoModule
        return DynamicMongoModule

    @staticmethod
    def for_feature(collections: list[str]) -> type:
        cache_key = tuple(collections)
        if cache_key in _feature_module_cache:
            return _feature_module_cache[cache_key]
        providers = [
            provider_factory(
                collection_token(name),
                lambda service, name=name: service.collection(name),
                inject=[MongoService],
            )
            for name in collections
        ]

        @Module(providers=providers, exports=[collection_token(name) for name in collections])
        class DynamicMongoFeatureModule:
            pass

        _feature_module_cache[cache_key] = DynamicMongoFeatureModule
        return DynamicMongoFeatureModule


MongooseModule = MongoModule
