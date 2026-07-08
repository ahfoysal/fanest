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
