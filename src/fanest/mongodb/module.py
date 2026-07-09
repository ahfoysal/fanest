from copy import deepcopy
from typing import Any
from uuid import uuid4

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token, use_factory

MONGO_OPTIONS = token("MONGO_OPTIONS")


def collection_token(name: str):
    return token(f"MONGO_COLLECTION:{name}")


def InjectModel(name: str):
    return Inject(collection_token(name))


class MongoCollection:
    def __init__(self, name: str):
        self.name = name
        self._documents: dict[str, dict[str, Any]] = {}

    async def insert_one(self, document: dict[str, Any]) -> dict[str, Any]:
        stored = deepcopy(document)
        stored.setdefault("_id", str(uuid4()))
        self._documents[str(stored["_id"])] = stored
        return deepcopy(stored)

    async def find(self, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query = query or {}
        return [deepcopy(document) for document in self._documents.values() if self._matches(document, query)]

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for document in await self.find(query):
            return document
        return None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> dict[str, Any] | None:
        match = await self.find_one(query)
        if match is None:
            return None
        stored = self._documents[str(match["_id"])]
        if any(str(key).startswith("$") for key in update):
            allowed = {"$set", "$unset", "$inc"}
            unsupported = set(update) - allowed
            if unsupported:
                raise ValueError(f"Unsupported Mongo update operator(s): {', '.join(sorted(unsupported))}")
            for key, value in update.get("$set", {}).items():
                stored[key] = value
            for key in update.get("$unset", {}):
                stored.pop(key, None)
            for key, value in update.get("$inc", {}).items():
                stored[key] = stored.get(key, 0) + value
        else:
            stored.update(update)
        return deepcopy(stored)

    async def delete_one(self, query: dict[str, Any]) -> bool:
        match = await self.find_one(query)
        if match is None:
            return False
        self._documents.pop(str(match["_id"]), None)
        return True

    def clear(self) -> None:
        self._documents.clear()

    def _matches(self, document: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(document.get(key) == value for key, value in query.items())


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

    async def find(self, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        cursor = self._collection.find(query or {})
        return [document async for document in cursor]

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return await self._collection.find_one(query)

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> dict[str, Any] | None:
        changes = update if any(str(k).startswith("$") for k in update) else {"$set": update}
        await self._collection.update_one(query, changes)
        return await self.find_one(query)

    async def delete_one(self, query: dict[str, Any]) -> bool:
        result = await self._collection.delete_one(query)
        return result.deleted_count > 0

    async def clear(self) -> None:
        await self._collection.delete_many({})


@Injectable()
class MongoService:
    def __init__(self, options: dict[str, Any] = Inject(MONGO_OPTIONS)):
        self.options = options
        self._collections: dict[str, Any] = {}
        self._client = None
        self._db = None
        uri = options.get("uri") or options.get("url")
        if uri:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover - exercised without motor installed
                raise ImportError(
                    "MongoModule.for_root(uri=...) requires the 'motor' package. "
                    "Install it with: pip install 'fanest[mongo]'"
                ) from exc
            self._client = AsyncIOMotorClient(uri)
            self._db = self._client[options.get("database", "fanest")]

    def collection(self, name: str) -> Any:
        if name not in self._collections:
            if self._db is not None:
                self._collections[name] = MotorCollection(self._db[name])
            else:
                self._collections[name] = MongoCollection(name)
        return self._collections[name]

    async def on_application_shutdown(self) -> None:
        if self._client is not None:
            self._client.close()


class MongoModule:
    @staticmethod
    def for_root(is_global: bool = True, **options: Any) -> type:
        @Module(
            providers=[use_value(MONGO_OPTIONS, options), MongoService],
            exports=[MongoService],
            global_module=is_global,
        )
        class DynamicMongoModule:
            pass

        return DynamicMongoModule

    @staticmethod
    def for_feature(collections: list[str]) -> type:
        providers = [
            use_factory(
                collection_token(name),
                lambda service, name=name: service.collection(name),
                inject=[MongoService],
            )
            for name in collections
        ]

        @Module(providers=providers, exports=[collection_token(name) for name in collections])
        class DynamicMongoFeatureModule:
            pass

        return DynamicMongoFeatureModule


MongooseModule = MongoModule
