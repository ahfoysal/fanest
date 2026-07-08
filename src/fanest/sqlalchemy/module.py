from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token, use_factory

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


class SqlAlchemyRepository:
    def __init__(self, service: SqlAlchemyService, model: type):
        self.service = service
        self.model = model

    async def find_all(self) -> list[Any]:
        async for session in self.service.session():
            result = await session.execute(select(self.model))
            return list(result.scalars().all())
        return []

    async def find_one(self, primary_key: Any) -> Any | None:
        async for session in self.service.session():
            return await session.get(self.model, primary_key)
        return None

    async def save(self, entity: Any) -> Any:
        async for session in self.service.session():
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            return entity
        return entity

    async def delete(self, entity: Any) -> None:
        async for session in self.service.session():
            await session.delete(entity)
            await session.commit()


def repository_token(model: type):
    return token(f"SQLALCHEMY_REPOSITORY:{model.__module__}.{model.__name__}")


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
    def for_feature(models: list[type]) -> type:
        providers = [
            use_factory(
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
