import os

import pytest
from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Inject, Module, Post
from fanest.mongodb import InjectModel, MongoCollection, MongoModule, MongoService, MongooseModule, collection_token
from fanest.mongodb.module import MotorCollection


USERS_COLLECTION = collection_token("users")


class FakeMotorResult:
    def __init__(self, *, modified_count: int = 0, deleted_count: int = 0):
        self.modified_count = modified_count
        self.deleted_count = deleted_count


class FakeMotorCursor:
    def __init__(self, collection: MongoCollection, query, projection):
        self.collection = collection
        self.query = query
        self.projection = projection
        self.sort_value = None
        self.skip_value = 0
        self.limit_value = None

    def sort(self, *args):
        if len(args) == 2:
            self.sort_value = (args[0], args[1])
        else:
            self.sort_value = args[0]
        return self

    def skip(self, value):
        self.skip_value = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    async def _items(self):
        return await self.collection.find(
            self.query,
            sort=self.sort_value,
            skip=self.skip_value,
            limit=self.limit_value,
            projection=self.projection,
        )

    def __aiter__(self):
        async def iterate():
            for item in await self._items():
                yield item

        return iterate()


class FakeMotorCollection:
    def __init__(self, name: str):
        self.memory = MongoCollection(name)

    async def insert_one(self, document):
        return await self.memory.insert_one(document)

    async def insert_many(self, documents):
        return await self.memory.insert_many(documents)

    def find(self, query, projection=None):
        return FakeMotorCursor(self.memory, query, projection)

    async def find_one(self, query):
        return await self.memory.find_one(query)

    async def update_one(self, query, update):
        updated = await self.memory.update_one(query, update)
        return FakeMotorResult(modified_count=1 if updated is not None else 0)

    async def update_many(self, query, update):
        return FakeMotorResult(modified_count=await self.memory.update_many(query, update))

    async def delete_one(self, query):
        return FakeMotorResult(deleted_count=1 if await self.memory.delete_one(query) else 0)

    async def delete_many(self, query):
        return FakeMotorResult(deleted_count=await self.memory.delete_many(query))

    async def count_documents(self, query):
        return await self.memory.count_documents(query)

    async def distinct(self, field, query):
        return await self.memory.distinct(field, query)


class FakeMotorDatabase:
    def __init__(self):
        self.collections: dict[str, FakeMotorCollection] = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeMotorCollection(name))


class FakeMotorClient:
    def __init__(self):
        self.databases: dict[str, FakeMotorDatabase] = {}
        self.closed = False

    def __getitem__(self, name):
        return self.databases.setdefault(name, FakeMotorDatabase())

    def close(self):
        self.closed = True


@Controller("mongo-users")
class MongoUsersController:
    def __init__(self, users: MongoCollection = Inject(USERS_COLLECTION)):
        self.users = users

    @Post("/")
    async def create(self):
        return await self.users.insert_one({"email": "ada@example.com", "name": "Ada"})

    @Get("/")
    async def find_all(self):
        return await self.users.find()


@Module(
    imports=[MongoModule.for_root(database="fanest"), MongoModule.for_feature(["users"])],
    controllers=[MongoUsersController],
)
class MongoAppModule:
    pass


def test_mongodb_module_registers_injectable_collections():
    client = TestClient(FaNestFactory.create(MongoAppModule))

    created = client.post("/mongo-users").json()
    users = client.get("/mongo-users").json()

    assert created["email"] == "ada@example.com"
    assert users == [created]


@Controller("mongoose-users")
class MongooseUsersController:
    def __init__(self, users: MongoCollection = InjectModel("users")):
        self.users = users

    @Post("/")
    async def create(self):
        return await self.users.insert_one({"email": "grace@example.com", "name": "Grace"})


@Module(
    imports=[MongooseModule.for_root(database="fanest"), MongooseModule.for_feature(["users"])],
    controllers=[MongooseUsersController],
)
class MongooseAppModule:
    pass


def test_mongoose_alias_and_inject_model_helper():
    created = TestClient(FaNestFactory.create(MongooseAppModule)).post("/mongoose-users").json()

    assert created["name"] == "Grace"


async def _exercise_mongo_update_operators():
    collection = MongoCollection("users")
    created = await collection.insert_one({"email": "lin@example.com", "name": "Lin", "logins": 1})

    updated = await collection.update_one({"_id": created["_id"]}, {"$set": {"name": "Linus"}, "$inc": {"logins": 2}})
    assert updated is not None
    assert updated["name"] == "Linus"
    assert updated["logins"] == 3

    updated = await collection.update_one({"_id": created["_id"]}, {"$unset": {"email": ""}})
    assert updated is not None
    assert "email" not in updated

    updated = await collection.update_one({"_id": created["_id"]}, {"$push": {"roles": "admin"}})
    assert updated is not None
    assert updated["roles"] == ["admin"]

    updated = await collection.update_one({"_id": created["_id"]}, {"$addToSet": {"roles": "admin"}})
    assert updated is not None
    assert updated["roles"] == ["admin"]

    updated = await collection.update_one({"_id": created["_id"]}, {"$pull": {"roles": "admin"}})
    assert updated is not None
    assert updated["roles"] == []

    try:
        await collection.update_one({"_id": created["_id"]}, {"$rename": {"name": "displayName"}})
    except ValueError:
        pass
    else:  # pragma: no cover - makes the assertion message clearer on failure
        raise AssertionError("unsupported Mongo update operator should raise")

    stored = await collection.find_one({"_id": created["_id"]})
    assert stored is not None
    assert "$rename" not in stored


def test_in_memory_mongo_update_operators_are_safe():
    import asyncio

    asyncio.run(_exercise_mongo_update_operators())


async def _exercise_mongo_query_operators_and_nested_paths():
    collection = MongoCollection("users")
    await collection.insert_one(
        {
            "email": "ada@example.com",
            "profile": {"age": 36, "city": "London"},
            "roles": ["admin", "writer"],
        }
    )
    await collection.insert_one(
        {
            "email": "grace@example.com",
            "profile": {"age": 42, "city": "Arlington"},
            "roles": ["operator"],
        }
    )

    assert [item["email"] for item in await collection.find({"profile.age": {"$gte": 40}})] == [
        "grace@example.com"
    ]
    assert [item["email"] for item in await collection.find({"email": {"$regex": "^ada"}})] == ["ada@example.com"]
    assert [item["email"] for item in await collection.find({"$or": [{"profile.city": "London"}, {"roles": "operator"}]})] == [
        "ada@example.com",
        "grace@example.com",
    ]
    assert [item["email"] for item in await collection.find({"profile.country": {"$exists": False}})] == [
        "ada@example.com",
        "grace@example.com",
    ]

    updated = await collection.update_one(
        {"email": "ada@example.com"},
        {"$set": {"profile.country": "UK"}, "$inc": {"profile.age": 1}},
    )
    assert updated is not None
    assert updated["profile"]["country"] == "UK"
    assert updated["profile"]["age"] == 37


def test_in_memory_mongo_query_operators_and_nested_paths():
    import asyncio

    asyncio.run(_exercise_mongo_query_operators_and_nested_paths())


async def _exercise_mongo_batch_projection_and_counts():
    collection = MongoCollection("users")
    await collection.insert_many(
        [
            {"email": "ada@example.com", "profile": {"age": 36}, "roles": ["admin", "writer"]},
            {"email": "grace@example.com", "profile": {"age": 42}, "roles": ["operator"]},
            {"email": "linus@example.com", "profile": {"age": 55}, "roles": ["admin"]},
        ]
    )

    assert await collection.count_documents({"roles": "admin"}) == 2
    assert [item["email"] for item in await collection.find(sort=("profile.age", -1), skip=1, limit=1)] == [
        "grace@example.com"
    ]
    projected = await collection.find({"email": "ada@example.com"}, projection={"email": 1, "_id": 0})
    assert projected == [{"email": "ada@example.com"}]
    excluded = await collection.find({"email": "ada@example.com"}, projection={"roles": 0})
    assert "roles" not in excluded[0]
    assert "profile" in excluded[0]

    assert await collection.distinct("roles") == ["admin", "writer", "operator"]
    assert await collection.update_many({"roles": "admin"}, {"$inc": {"profile.age": 1}}) == 2
    assert [item["profile"]["age"] for item in await collection.find({"roles": "admin"}, sort=("email", 1))] == [
        37,
        56,
    ]
    assert await collection.delete_many({"profile.age": {"$gte": 50}}) == 1
    assert await collection.count_documents({}) == 2


def test_in_memory_mongo_batch_projection_and_counts():
    import asyncio

    asyncio.run(_exercise_mongo_batch_projection_and_counts())


async def _exercise_motor_wrapper_contract():
    collection = MotorCollection(FakeMotorCollection("users"))

    await collection.insert_many(
        [
            {"email": "ada@example.com", "profile": {"age": 36}, "roles": ["admin", "writer"]},
            {"email": "grace@example.com", "profile": {"age": 42}, "roles": ["operator"]},
            {"email": "linus@example.com", "profile": {"age": 55}, "roles": ["admin"]},
        ]
    )

    projected = await collection.find(
        {"roles": "admin"},
        projection={"email": 1, "_id": 0},
        sort=("profile.age", -1),
        skip=1,
        limit=1,
    )
    assert projected == [{"email": "ada@example.com"}]

    assert await collection.update_many({"roles": "admin"}, {"$inc": {"profile.age": 1}}) == 2
    assert await collection.count_documents({"profile.age": {"$gte": 40}}) == 2
    assert await collection.distinct("roles") == ["admin", "writer", "operator"]
    assert await collection.delete_many({"roles": "operator"}) == 1
    assert await collection.count_documents({}) == 2


def test_motor_collection_wrapper_matches_mongo_collection_contract():
    import asyncio

    asyncio.run(_exercise_motor_wrapper_contract())


def test_mongo_module_accepts_motor_style_client_and_closes_it():
    import asyncio

    client = FakeMotorClient()

    @Module(imports=[MongoModule.for_root(client=client, database="fanest"), MongoModule.for_feature(["users"])])
    class MotorClientMongoModule:
        pass

    async def run():
        app = FaNestFactory.create(MotorClientMongoModule)
        service = await app.state.fanest_container.resolve_async(MongoService)
        users = await app.state.fanest_container.resolve_async(collection_token("users"))

        created = await users.insert_one({"email": "ada@example.com"})

        assert await users.find_one({"email": "ada@example.com"}) == created

        await service.on_application_shutdown()
        assert client.closed is True
        with pytest.raises(RuntimeError):
            service.collection("users")

    asyncio.run(run())


@pytest.mark.live_mongo
@pytest.mark.skipif(not os.getenv("FANEST_LIVE_MONGO"), reason="set FANEST_LIVE_MONGO to run live MongoDB checks")
def test_live_mongo_motor_contract_when_enabled():
    import asyncio

    async def run():
        uri = os.getenv("FANEST_LIVE_MONGO_URL", "mongodb://localhost:27017")
        module = MongoModule.for_root(uri=uri, database="fanest_live_tests")
        app = FaNestFactory.create(module)
        service = await app.state.fanest_container.resolve_async(MongoService)
        users = service.collection("users")
        await users.clear()
        created = await users.insert_one({"email": "live@example.com", "roles": ["live"]})
        assert await users.find_one({"_id": created["_id"]}) == created
        await users.clear()
        await service.on_application_shutdown()

    asyncio.run(run())


def test_mongo_dynamic_modules_are_stable_for_identical_options():
    assert MongoModule.for_root(database="fanest") is MongoModule.for_root(database="fanest")
    assert MongoModule.for_feature(["users"]) is MongoModule.for_feature(["users"])

    async def options_factory():
        return {"database": "fanest"}

    assert MongoModule.for_root_async(use_factory=options_factory) is MongoModule.for_root_async(
        use_factory=options_factory
    )


async def _mongo_async_options_factory():
    return {"database": "async-fanest"}


@Module(
    imports=[MongoModule.for_root_async(use_factory=_mongo_async_options_factory), MongoModule.for_feature(["async_users"])],
)
class AsyncMongoAppModule:
    pass


def test_mongo_for_root_async_wires_service_and_collections():
    import asyncio

    async def run():
        app = FaNestFactory.create(AsyncMongoAppModule)
        service = await app.state.fanest_container.resolve_async(MongoService)
        collection = await app.state.fanest_container.resolve_async(collection_token("async_users"))

        assert service.options["database"] == "async-fanest"
        created = await collection.insert_one({"email": "async@example.com"})
        assert await collection.find_one({"_id": created["_id"]}) == created

        await service.on_application_shutdown()
        try:
            service.collection("late")
        except RuntimeError:
            pass
        else:  # pragma: no cover - clearer failure message
            raise AssertionError("closed MongoService should reject new collection access")

    asyncio.run(run())
