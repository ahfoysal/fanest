from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Inject, Module, Post
from fanest.mongodb import InjectModel, MongoCollection, MongoModule, MongooseModule, collection_token


USERS_COLLECTION = collection_token("users")


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

    try:
        await collection.update_one({"_id": created["_id"]}, {"$push": {"roles": "admin"}})
    except ValueError:
        pass
    else:  # pragma: no cover - makes the assertion message clearer on failure
        raise AssertionError("unsupported Mongo update operator should raise")

    stored = await collection.find_one({"_id": created["_id"]})
    assert stored is not None
    assert "$push" not in stored


def test_in_memory_mongo_update_operators_are_safe():
    import asyncio

    asyncio.run(_exercise_mongo_update_operators())
