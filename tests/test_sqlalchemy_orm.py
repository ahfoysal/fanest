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
    assert await service.users.delete_by(email="ada@example.com") == 1
    assert await service.users.count() == 2
    with pytest.raises(LookupError):
        await service.users.find_one_or_fail(999)
    await db.close()


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
