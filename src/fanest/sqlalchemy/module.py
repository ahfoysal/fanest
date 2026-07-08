from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy import func, select
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

SQLALCHEMY_OPTIONS = token("SQLALCHEMY_OPTIONS")


@Injectable()
class SqlAlchemyService:
    def __init__(self, options: dict[str, Any] = Inject(SQLALCHEMY_OPTIONS)):
        self._engine = create_async_engine(options["database_url"], echo=options.get("echo", False))
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("SqlAlchemyModule.for_root(...) has not been configured.")
        return self._engine

    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("SqlAlchemyModule.for_root(...) has not been configured.")
        async with self._sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("SqlAlchemyModule.for_root(...) has not been configured.")
        async with self._sessionmaker() as session:
            async with session.begin():
                yield session

    def create_repository(self, model: type) -> "SqlAlchemyRepository":
        return SqlAlchemyRepository(self, model)

    async def close(self) -> None:
        await self._engine.dispose()

    async def on_application_shutdown(self) -> None:
        await self.close()


class SqlAlchemyRepository:
    def __init__(self, service: SqlAlchemyService, model: type):
        self.service = service
        self.model = model

    async def find_all(self) -> list[Any]:
        async for session in self.service.session():
            result = await session.execute(select(self.model))
            return list(result.scalars().all())
        return []

    async def find_by(self, **criteria: Any) -> list[Any]:
        async for session in self.service.session():
            result = await session.execute(select(self.model).where(*self._filters(criteria)))
            return list(result.scalars().all())
        return []

    async def find_one(self, primary_key: Any) -> Any | None:
        async for session in self.service.session():
            return await session.get(self.model, primary_key)
        return None

    async def find_one_by(self, **criteria: Any) -> Any | None:
        async for session in self.service.session():
            result = await session.execute(select(self.model).where(*self._filters(criteria)).limit(1))
            return result.scalars().first()
        return None

    async def count(self, **criteria: Any) -> int:
        async for session in self.service.session():
            statement = select(func.count()).select_from(self.model)
            if criteria:
                statement = statement.where(*self._filters(criteria))
            result = await session.execute(statement)
            return int(result.scalar_one())
        return 0

    async def save(self, entity: Any) -> Any:
        async for session in self.service.session():
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            return entity
        return entity

    async def update(self, criteria: dict[str, Any], values: dict[str, Any]) -> int:
        async for session in self.service.session():
            result = await session.execute(
                sqlalchemy_update(self.model).where(*self._filters(criteria)).values(**values)
            )
            await session.commit()
            return int(result.rowcount or 0)
        return 0

    async def delete(self, entity: Any) -> None:
        async for session in self.service.session():
            await session.delete(entity)
            await session.commit()

    async def delete_by(self, **criteria: Any) -> int:
        async for session in self.service.session():
            result = await session.execute(sqlalchemy_delete(self.model).where(*self._filters(criteria)))
            await session.commit()
            return int(result.rowcount or 0)
        return 0

    def _filters(self, criteria: dict[str, Any]) -> list[Any]:
        return [getattr(self.model, key) == value for key, value in criteria.items()]


def repository_token(model: type):
    return token(f"SQLALCHEMY_REPOSITORY:{model.__module__}.{model.__name__}")


def InjectRepository(model: type):
    return Inject(repository_token(model))


def Transactional(service_attr: str = "db"):
    def decorator(handler):
        @wraps(handler)
        async def wrapper(self, *args, **kwargs):
            service = getattr(self, service_attr)
            async with service.transaction() as session:
                kwargs.setdefault("session", session)
                return await handler(self, *args, **kwargs)

        return wrapper

    return decorator


class MigrationManager:
    def __init__(self, directory: str | Path = "migrations") -> None:
        self.directory = Path(directory)

    def create(self, name: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        slug = name.lower().replace(" ", "_")
        existing = sorted(self.directory.glob("*.py"))
        filename = f"{len(existing) + 1:04d}_{slug}.py"
        path = self.directory / filename
        path.write_text(self.template(name), encoding="utf-8")
        return path

    def template(self, name: str) -> str:
        return f'''"""Migration: {name}."""


async def upgrade(connection):
    pass


async def downgrade(connection):
    pass
'''


class SqlAlchemyModule:
    @staticmethod
    def for_root(*, database_url: str, echo: bool = False) -> type:
        options = {"database_url": database_url, "echo": echo}

        @Module(
            providers=[use_value(SQLALCHEMY_OPTIONS, options), SqlAlchemyService],
            exports=[SqlAlchemyService],
        )
        class DynamicSqlAlchemyModule:
            pass

        return DynamicSqlAlchemyModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any]],
        inject: list[Any] | None = None,
    ) -> type:
        @Module(
            providers=[
                provider_factory(SQLALCHEMY_OPTIONS, use_factory, inject=inject or []),
                SqlAlchemyService,
            ],
            exports=[SqlAlchemyService],
        )
        class DynamicSqlAlchemyModule:
            pass

        return DynamicSqlAlchemyModule

    @staticmethod
    def for_feature(models: list[type]) -> type:
        providers = [
            provider_factory(
                repository_token(model),
                lambda service, model=model: service.create_repository(model),
                inject=[SqlAlchemyService],
            )
            for model in models
        ]

        @Module(providers=providers, exports=[repository_token(model) for model in models])
        class DynamicSqlAlchemyFeatureModule:
            pass

        return DynamicSqlAlchemyFeatureModule


TypeOrmModule = SqlAlchemyModule
