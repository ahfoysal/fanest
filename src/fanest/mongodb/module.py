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
        stored.update(update.get("$set", update))
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


@Injectable()
class MongoService:
    def __init__(self, options: dict[str, Any] = Inject(MONGO_OPTIONS)):
        self.options = options
        self._collections: dict[str, MongoCollection] = {}

    def collection(self, name: str) -> MongoCollection:
        if name not in self._collections:
            self._collections[name] = MongoCollection(name)
        return self._collections[name]


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
