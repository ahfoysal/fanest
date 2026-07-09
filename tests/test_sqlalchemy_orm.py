import pytest
from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fanest import FaNestFactory, Injectable, Module
from fanest.sqlalchemy import (
    InjectConnection,
    InjectDataSource,
    InjectEntityManager,
    InjectManager,
    InjectRepository,
    MikroOrmModule,
    PrismaModule,
    SequelizeModule,
    SqlAlchemyRepository,
    SqlAlchemyService,
    Transaction,
    TypeOrmModule,
    UnsupportedDatabaseRecipeError,
    get_current_session,
    get_connection_token,
    get_data_source_token,
    get_entity_manager_token,
    get_repository_token,
    repository_token,
)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "orm_users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    age: Mapped[int | None] = mapped_column(default=None)


@Injectable()
class UsersOrmService:
    def __init__(self, users: SqlAlchemyRepository = InjectRepository(User)):
        self.users = users

    async def create(self, email: str, name: str) -> User:
        return await self.users.save(User(email=email, name=name))


@Injectable()
class UsersEntityManagerService:
    def __init__(self, db: SqlAlchemyService = InjectEntityManager()):
        self.db = db


@Injectable()
class UsersTypeOrmAliasService:
    def __init__(
        self,
        data_source: SqlAlchemyService = InjectDataSource(),
        connection: SqlAlchemyService = InjectConnection(),
        manager: SqlAlchemyService = InjectManager(),
    ):
        self.data_source = data_source
        self.connection = connection
        self.manager = manager

    @Transaction("data_source")
    async def create_in_transaction(self, users: SqlAlchemyRepository, *, session=None) -> int:
        assert session is get_current_session()
        await users.save(User(email="decorator@example.com", name="Decorator"))
        return await users.count()


def make_orm_module(database_url: str) -> type:
    @Module(
        imports=[
            TypeOrmModule.for_root(database_url=database_url),
            TypeOrmModule.for_feature([User]),
        ],
        providers=[UsersOrmService],
    )
    class OrmAppModule:
        pass

    return OrmAppModule


def make_orm_module_with_manager(database_url: str) -> type:
    @Module(
        imports=[
            TypeOrmModule.for_root(database_url=database_url),
            TypeOrmModule.for_feature((User,)),
        ],
        providers=[UsersEntityManagerService],
    )
    class OrmManagerAppModule:
        pass

    return OrmManagerAppModule


def make_orm_module_with_aliases(database_url: str) -> type:
    @Module(
        imports=[
            TypeOrmModule.for_root(database_url=database_url),
            TypeOrmModule.for_feature([User]),
        ],
        providers=[UsersTypeOrmAliasService],
    )
    class OrmAliasAppModule:
        pass

    return OrmAliasAppModule


@pytest.mark.anyio
async def test_typeorm_module_alias_and_inject_repository(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    app = FaNestFactory.create(make_orm_module(database_url))
    container = app.state.fanest_container
    db = container.resolve(SqlAlchemyService)

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    service = container.resolve(UsersOrmService)
    created = await service.create("ada@example.com", "Ada")

    assert created.id == 1
    assert await service.users.count() == 1
    assert await service.users.exists(email="ada@example.com") is True
    assert await service.users.find_one(created.id) is not None
    assert await service.users.find_one_or_fail(created.id) is not None
    assert (await service.users.find_one_by(email="ada@example.com")).name == "Ada"
    assert [user.email for user in await service.users.find_by(name="Ada")] == ["ada@example.com"]
    assert await service.users.update({"email": "ada@example.com"}, {"name": "Ada Lovelace"}) == 1
    assert (await service.users.find_one_by(email="ada@example.com")).name == "Ada Lovelace"
    await service.users.insert_many(
        [
            User(email="grace@example.com", name="Grace"),
            User(email="linus@example.com", name="Linus"),
        ]
    )
    assert [user.name for user in await service.users.find(order_by=("name", "desc"), take=2)] == [
        "Linus",
        "Grace",
    ]
    assert [user.name for user in await service.users.find(where={"name": {"$in": ["Grace", "Linus"]}})] == [
        "Grace",
        "Linus",
    ]
    assert await service.users.count(name={"$ne": "Ada Lovelace"}) == 2
    assert await service.users.delete_by(email="ada@example.com") == 1
    assert await service.users.count() == 2
    with pytest.raises(LookupError):
        await service.users.find_one_or_fail(999)
    await db.close()


@pytest.mark.anyio
async def test_sqlalchemy_repository_advanced_filters_and_upsert(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'advanced.db'}"
    app = FaNestFactory.create(make_orm_module(database_url))
    container = app.state.fanest_container
    db = container.resolve(SqlAlchemyService)

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    service = container.resolve(UsersOrmService)
    await service.users.save_many(
        service.users.create_many(
            [
                {"email": "ada@example.com", "name": "Ada", "age": 36},
                {"email": "grace@example.com", "name": "Grace", "age": 42},
                {"email": "linus@example.com", "name": "Linus", "age": None},
            ]
        )
    )

    rows, total = await service.users.find_and_count(
        where={"$or": [{"age": {"$between": (35, 40)}}, {"age": {"$isnull": True}}]},
        order_by="email",
        take=1,
    )
    assert [user.email for user in rows] == ["ada@example.com"]
    assert total == 2
    assert await service.users.count_where({"$and": [{"name": {"$like": "G%"}}, {"age": {"$gte": 40}}]}) == 1
    assert (await service.users.find_one_where({"age": {"$isnull": True}})).email == "linus@example.com"
    assert (await service.users.find_one_or_fail_by(email="grace@example.com")).name == "Grace"

    transient = service.users.create(email="draft@example.com", name="Draft", age=1)
    service.users.merge(transient, {"name": "Merged"}, age=2)
    assert transient.name == "Merged"
    assert transient.age == 2

    created = await service.users.upsert({"email": "new@example.com"}, {"name": "New", "age": 18})
    updated = await service.users.upsert({"email": "new@example.com"}, {"name": "Updated", "age": 19})

    assert created.id == updated.id
    assert updated.name == "Updated"
    assert await service.users.remove(updated) is updated
    assert await service.users.exists(email="new@example.com") is False
    await service.users.save(User(email="new@example.com", name="Again", age=20))
    assert await service.users.clear() == 4
    assert await service.users.count() == 0
    await db.close()

    with pytest.raises(RuntimeError):
        async for _session in db.session():
            pass


@pytest.mark.anyio
async def test_repository_operations_join_active_transaction(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'tx.db'}"
    app = FaNestFactory.create(make_orm_module(database_url))
    container = app.state.fanest_container
    db = container.resolve(SqlAlchemyService)

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    service = container.resolve(UsersOrmService)

    with pytest.raises(RuntimeError):
        async with db.transaction():
            await service.users.save(User(email="rollback@example.com", name="Rollback"))
            raise RuntimeError("rollback")

    assert await service.users.count() == 0
    async with db.transaction():
        await service.users.save(User(email="commit@example.com", name="Commit"))

    assert await service.users.exists(email="commit@example.com") is True
    await db.close()


@pytest.mark.anyio
async def test_sqlalchemy_lifecycle_is_idempotent_and_repositories_reject_after_shutdown(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'shutdown.db'}"
    app = FaNestFactory.create(make_orm_module(database_url))
    container = app.state.fanest_container
    db = container.resolve(SqlAlchemyService)

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    service = container.resolve(UsersOrmService)
    await service.users.save(User(email="ada@example.com", name="Ada"))

    await db.on_application_shutdown()
    await db.on_application_shutdown()

    with pytest.raises(RuntimeError):
        await service.users.find_all()


@pytest.mark.anyio
async def test_sqlalchemy_transaction_scope_recovers_after_rollback(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'rollback-recovery.db'}"
    app = FaNestFactory.create(make_orm_module(database_url))
    container = app.state.fanest_container
    db = container.resolve(SqlAlchemyService)

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    service = container.resolve(UsersOrmService)

    with pytest.raises(ValueError):
        async with db.transaction():
            await service.users.save(User(email="rolled@example.com", name="Rolled"))
            assert await service.users.count(email="rolled@example.com") == 1
            raise ValueError("rollback")

    assert await service.users.count(email="rolled@example.com") == 0
    await service.users.save(User(email="after@example.com", name="After"))
    assert await service.users.exists(email="after@example.com") is True
    await db.close()


@pytest.mark.anyio
async def test_nested_sqlalchemy_transaction_reuses_active_session(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'nested-tx.db'}"
    app = FaNestFactory.create(make_orm_module(database_url))
    container = app.state.fanest_container
    db = container.resolve(SqlAlchemyService)

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    service = container.resolve(UsersOrmService)

    async with db.transaction() as outer:
        async with db.transaction() as inner:
            assert inner is outer
            await service.users.save(User(email="nested@example.com", name="Nested"))
            assert await service.users.count(email="nested@example.com") == 1

    assert await service.users.exists(email="nested@example.com") is True
    await db.close()


def test_sqlalchemy_dynamic_modules_are_stable_for_identical_options():
    database_url = "sqlite+aiosqlite:///:memory:"

    assert TypeOrmModule.for_root(database_url=database_url) is TypeOrmModule.for_root(database_url=database_url)
    assert TypeOrmModule.for_feature([User]) is TypeOrmModule.for_feature([User])
    assert get_repository_token(User) == repository_token(User)
    assert get_repository_token(User, "default") == repository_token(User)
    assert get_connection_token() == get_data_source_token("default")
    assert get_data_source_token() != get_entity_manager_token()
    assert get_data_source_token() != get_repository_token(User)
    with pytest.raises(UnsupportedDatabaseRecipeError, match="Named TypeORM data sources"):
        get_repository_token(User, "reporting")

    async def options_factory():
        return {"database_url": database_url}

    assert TypeOrmModule.for_root_async(use_factory=options_factory) is TypeOrmModule.for_root_async(
        use_factory=options_factory
    )


@pytest.mark.anyio
async def test_sqlalchemy_session_helpers_and_entity_manager_injection(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'session-helpers.db'}"
    app = FaNestFactory.create(make_orm_module_with_manager(database_url))
    container = app.state.fanest_container
    manager = container.resolve(UsersEntityManagerService)

    async with manager.db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    repository = manager.db.get_repository(User)

    async with manager.db.session_scope() as session:
        session.add(User(email="scope@example.com", name="Scope"))
        await session.commit()

    async def create_in_transaction(session):
        assert get_current_session() is session
        await repository.save(User(email="tx-helper@example.com", name="Tx Helper"))
        return await repository.count()

    assert await manager.db.run_in_transaction(create_in_transaction) == 2
    assert get_current_session() is None
    assert await repository.exists(email="tx-helper@example.com") is True
    await manager.db.close()


@pytest.mark.anyio
async def test_typeorm_data_source_connection_and_transaction_aliases(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'typeorm-aliases.db'}"
    app = FaNestFactory.create(make_orm_module_with_aliases(database_url))
    container = app.state.fanest_container
    db = container.resolve(SqlAlchemyService)

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    service = container.resolve(UsersTypeOrmAliasService)
    repository = container.resolve(get_repository_token(User))

    assert service.data_source is db
    assert service.connection is db
    assert service.manager is db
    assert container.resolve(get_data_source_token()) is db
    assert container.resolve(get_entity_manager_token()) is db
    assert await service.create_in_transaction(repository) == 1
    assert await repository.exists(email="decorator@example.com") is True
    await db.close()


def test_javascript_orm_recipe_modules_raise_clear_python_native_error():
    for module in (SequelizeModule, MikroOrmModule, PrismaModule):
        with pytest.raises(UnsupportedDatabaseRecipeError, match="Python-native"):
            module.for_root()


def test_javascript_orm_stub_packages_import_and_raise_clear_alternatives():
    from fanest.mikroorm import MikroOrmModule as StubMikroOrmModule
    from fanest.prisma import PrismaModule as StubPrismaModule
    from fanest.sequelize import SequelizeModule as StubSequelizeModule

    for module in (StubSequelizeModule, StubMikroOrmModule, StubPrismaModule):
        with pytest.raises(UnsupportedDatabaseRecipeError, match="SqlAlchemyModule / TypeOrmModule"):
            module.for_feature([])


def test_create_all_and_advisory_lock_are_multi_instance_safe():
    """create_all() runs under an advisory lock so concurrent replicas don't race;
    the in-process fallback (SQLite) serializes concurrent lock holders."""
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    class Base(DeclarativeBase):
        pass

    class Widget(Base):
        __tablename__ = "adv_widgets"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column(String(40))

    service = SqlAlchemyService({"database_url": "sqlite+aiosqlite:///:memory:"})

    async def scenario():
        # create_all builds the schema and is idempotent when called again
        await service.create_all(Base.metadata)
        await service.create_all(Base.metadata)
        async with service.session_scope() as session:
            count = (await session.execute(text("select count(*) from adv_widgets"))).scalar()

        # concurrent advisory_lock holders run one at a time (no interleaving)
        order: list[tuple[str, int]] = []

        async def worker(index: int) -> None:
            async with service.advisory_lock("bootstrap"):
                order.append(("enter", index))
                await asyncio.sleep(0.01)
                order.append(("exit", index))

        await asyncio.gather(*(worker(i) for i in range(4)))
        await service.close()
        return count, order

    count, order = asyncio.run(scenario())
    assert count == 0
    # every enter is immediately followed by the same worker's exit
    assert all(
        order[i][0] == "enter" and order[i + 1][0] == "exit" and order[i][1] == order[i + 1][1]
        for i in range(0, len(order), 2)
    )
    # advisory-lock key is a stable signed 64-bit int (pg_advisory_lock compatible)
    key = SqlAlchemyService._advisory_lock_key("bootstrap")
    assert -(2**63) <= key < 2**63
    assert key == SqlAlchemyService._advisory_lock_key("bootstrap")
