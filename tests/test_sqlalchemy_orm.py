import pytest
from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fanest import FaNestFactory, Injectable, Module
from fanest.sqlalchemy import InjectRepository, SqlAlchemyRepository, SqlAlchemyService, TypeOrmModule


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

    async def options_factory():
        return {"database_url": database_url}

    assert TypeOrmModule.for_root_async(use_factory=options_factory) is TypeOrmModule.for_root_async(
        use_factory=options_factory
    )
