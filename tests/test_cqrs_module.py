from dataclasses import dataclass

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module, Post
from fanest.cqrs import (
    CommandBus,
    CommandHandler,
    CqrsModule,
    EventBus,
    EventsHandler,
    QueryBus,
    QueryHandler,
)


@dataclass(frozen=True)
class CreateUserCommand:
    name: str


@dataclass(frozen=True)
class CountUsersQuery:
    pass


@dataclass(frozen=True)
class UserCreatedEvent:
    name: str


class UserStore:
    users: list[str] = []
    events: list[str] = []


@CommandHandler(CreateUserCommand)
class CreateUserHandler:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus

    async def execute(self, command: CreateUserCommand):
        UserStore.users.append(command.name)
        await self.event_bus.publish(UserCreatedEvent(command.name))
        return {"name": command.name}


@QueryHandler(CountUsersQuery)
class CountUsersHandler:
    def execute(self, query: CountUsersQuery):
        return len(UserStore.users)


@EventsHandler(UserCreatedEvent)
class UserCreatedHandler:
    def handle(self, event: UserCreatedEvent):
        UserStore.events.append(event.name)


@Controller("cqrs")
class CqrsController:
    def __init__(self, commands: CommandBus, queries: QueryBus):
        self.commands = commands
        self.queries = queries

    @Post("/")
    async def create(self):
        return await self.commands.execute(CreateUserCommand("Ada"))

    @Get("/")
    async def count(self):
        return {"count": await self.queries.execute(CountUsersQuery())}


@Module(
    imports=[CqrsModule.for_root()],
    controllers=[CqrsController],
    providers=[UserStore, CreateUserHandler, CountUsersHandler, UserCreatedHandler],
)
class CqrsAppModule:
    pass


def test_cqrs_command_query_and_event_buses():
    UserStore.users = []
    UserStore.events = []

    with TestClient(FaNestFactory.create(CqrsAppModule)) as client:
        assert client.post("/cqrs").json() == {"name": "Ada"}
        assert client.get("/cqrs").json() == {"count": 1}

    assert UserStore.events == ["Ada"]


def test_cqrs_event_handlers_are_not_duplicated_across_repeated_lifespan_startups():
    UserStore.users = []
    UserStore.events = []
    app = FaNestFactory.create(CqrsAppModule)

    with TestClient(app) as client:
        assert client.post("/cqrs").json() == {"name": "Ada"}
    with TestClient(app) as client:
        assert client.post("/cqrs").json() == {"name": "Ada"}

    assert UserStore.events == ["Ada", "Ada"]


@dataclass(frozen=True)
class ScopedCommand:
    pass


class ScopedCommandStore:
    created: list[int] = []


@Injectable(scope="request")
@CommandHandler(ScopedCommand)
class ScopedCommandHandler:
    created = 0

    def __init__(self):
        type(self).created += 1
        self.instance_id = type(self).created

    def execute(self, command: ScopedCommand):
        ScopedCommandStore.created.append(self.instance_id)
        return {"id": self.instance_id}


@Controller("scoped-cqrs")
class ScopedCqrsController:
    def __init__(self, commands: CommandBus):
        self.commands = commands

    @Post("/")
    async def create(self):
        return await self.commands.execute(ScopedCommand())


@Module(
    imports=[CqrsModule.for_root()],
    controllers=[ScopedCqrsController],
    providers=[ScopedCommandHandler],
)
class ScopedCqrsModule:
    pass


def test_cqrs_handlers_resolve_inside_request_scope():
    ScopedCommandHandler.created = 0
    ScopedCommandStore.created = []

    with TestClient(FaNestFactory.create(ScopedCqrsModule)) as client:
        assert client.post("/scoped-cqrs").json() == {"id": 1}
        assert client.post("/scoped-cqrs").json() == {"id": 2}

    assert ScopedCommandStore.created == [1, 2]


class SharedBusStore:
    command_bus_ids: list[int] = []


@Controller("cqrs-a")
class CqrsAController:
    def __init__(self, commands: CommandBus):
        self.commands = commands

    @Get("/")
    async def index(self):
        SharedBusStore.command_bus_ids.append(id(self.commands))
        return {"id": id(self.commands)}


@Controller("cqrs-b")
class CqrsBController:
    def __init__(self, commands: CommandBus):
        self.commands = commands

    @Get("/")
    async def index(self):
        SharedBusStore.command_bus_ids.append(id(self.commands))
        return {"id": id(self.commands)}


@Module(imports=[CqrsModule.for_root()], controllers=[CqrsAController])
class CqrsFeatureAModule:
    pass


@Module(imports=[CqrsModule.for_root()], controllers=[CqrsBController])
class CqrsFeatureBModule:
    pass


@Module(imports=[CqrsFeatureAModule, CqrsFeatureBModule])
class DuplicateCqrsImportModule:
    pass


def test_cqrs_for_root_reuses_single_bus_across_multiple_feature_imports():
    SharedBusStore.command_bus_ids = []

    with TestClient(FaNestFactory.create(DuplicateCqrsImportModule)) as client:
        first = client.get("/cqrs-a").json()["id"]
        second = client.get("/cqrs-b").json()["id"]

    assert first == second
    assert SharedBusStore.command_bus_ids == [first, second]
