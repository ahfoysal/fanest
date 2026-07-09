import asyncio
import hashlib
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, NoReturn

from sqlalchemy import and_, or_  # type: ignore[reportAttributeAccessIssue]
from sqlalchemy import delete as sqlalchemy_delete  # type: ignore[reportAttributeAccessIssue]
from sqlalchemy import func, select, text  # type: ignore[reportAttributeAccessIssue]
from sqlalchemy import update as sqlalchemy_update  # type: ignore[reportAttributeAccessIssue]
from sqlalchemy.ext.asyncio import (  # type: ignore[reportMissingImports]
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

SQLALCHEMY_OPTIONS = token("SQLALCHEMY_OPTIONS")
SQLALCHEMY_DATA_SOURCE = token("SQLALCHEMY_DATA_SOURCE")
SQLALCHEMY_ENTITY_MANAGER = token("SQLALCHEMY_ENTITY_MANAGER")
_current_session: ContextVar[AsyncSession | None] = ContextVar("fanest_sqlalchemy_session", default=None)
_root_module_cache: dict[tuple[str, bool, bool], type] = {}
_async_root_module_cache: dict[tuple[int, tuple[Any, ...], bool], type] = {}
_feature_module_cache: dict[tuple[type, ...], type] = {}


class UnsupportedDatabaseRecipeError(NotImplementedError):
    """Raised when a NestJS JavaScript ORM recipe has no Python-native adapter."""


def _ensure_default_data_source(name: str | None) -> None:
    if name not in {None, "default"}:
        raise UnsupportedDatabaseRecipeError(
            "Named TypeORM data sources are not implemented by FaNest's SQLAlchemy adapter. "
            "Use a separate SqlAlchemyModule.for_root(...) configuration and inject SqlAlchemyService instead."
        )


def get_current_session() -> AsyncSession | None:
    return _current_session.get()


@Injectable()
class SqlAlchemyService:
    def __init__(self, options: dict[str, Any] = Inject(SQLALCHEMY_OPTIONS)):
        self._engine = create_async_engine(options["database_url"], echo=options.get("echo", False))
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        self._closed = False
        self._advisory_locks: dict[str, asyncio.Lock] = {}

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("SqlAlchemyModule.for_root(...) has not been configured.")
        return self._engine

    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._closed:
            raise RuntimeError("SqlAlchemyService has been closed.")
        if self._sessionmaker is None:
            raise RuntimeError("SqlAlchemyModule.for_root(...) has not been configured.")
        async with self._sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        if self._closed:
            raise RuntimeError("SqlAlchemyService has been closed.")
        if self._sessionmaker is None:
            raise RuntimeError("SqlAlchemyModule.for_root(...) has not been configured.")
        active_session = _current_session.get()
        if active_session is not None:
            yield active_session
            return
        async with self._sessionmaker() as session:
            async with session.begin():
                token = _current_session.set(session)
                try:
                    yield session
                finally:
                    _current_session.reset(token)

    def create_repository(self, model: type) -> "SqlAlchemyRepository":
        return SqlAlchemyRepository(self, model)

    def get_repository(self, model: type) -> "SqlAlchemyRepository":
        return self.create_repository(model)

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        async for session in self.session():
            yield session

    async def run_in_transaction(self, handler: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        async with self.transaction() as session:
            return await handler(session)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._engine.dispose()

    async def on_application_shutdown(self) -> None:
        await self.close()

    @staticmethod
    def _advisory_lock_key(name: str) -> int:
        digest = hashlib.sha256(name.encode()).digest()
        # signed 64-bit integer, compatible with pg_advisory_lock(bigint)
        return int.from_bytes(digest[:8], "big", signed=True)

    @asynccontextmanager
    async def advisory_lock(self, name: str, *, timeout: float | None = None) -> AsyncIterator[None]:
        """Acquire a cross-instance lock so concurrent app replicas don't race on
        one-time work such as schema creation or migrations.

        Uses a database advisory lock on Postgres (``pg_advisory_lock``) and
        MySQL/MariaDB (``GET_LOCK``); on other backends (e.g. SQLite) it falls
        back to an in-process lock — safe for single-instance deployments. Wrap
        bootstrap/migration in it::

            async with db.advisory_lock("schema-bootstrap"):
                await db.create_all(Base.metadata)
        """
        if self._closed or self._engine is None:
            raise RuntimeError("SqlAlchemyModule.for_root(...) has not been configured.")
        dialect = self._engine.dialect.name
        key = self._advisory_lock_key(name)
        if dialect == "postgresql":
            connection = await self._engine.connect()
            try:
                await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
                yield
            finally:
                try:
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                finally:
                    await connection.close()
            return
        if dialect in {"mysql", "mariadb"}:
            connection = await self._engine.connect()
            lock_name = f"fanest:{key}"
            wait = -1 if timeout is None else int(timeout)
            try:
                await connection.execute(
                    text("SELECT GET_LOCK(:name, :timeout)"),
                    {"name": lock_name, "timeout": wait},
                )
                yield
            finally:
                try:
                    await connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                finally:
                    await connection.close()
            return
        lock = self._advisory_locks.setdefault(name, asyncio.Lock())
        async with lock:
            yield

    async def create_all(self, metadata: Any, *, lock: str | None = "fanest:schema") -> None:
        """Create all tables in ``metadata``, guarded by an advisory lock so that
        multiple instances booting at once don't race. ``create_all`` is
        idempotent, so replicas that lose the race simply no-op. Pass
        ``lock=None`` to skip locking (single-instance/dev)."""

        async def _run() -> None:
            async with self._engine.begin() as connection:
                await connection.run_sync(metadata.create_all)

        if lock is None:
            await _run()
            return
        async with self.advisory_lock(lock):
            await _run()


class SqlAlchemyRepository:
    def __init__(self, service: SqlAlchemyService, model: type):
        self.service = service
        self.model = model

    async def find_all(self) -> list[Any]:
        async with self._session() as session:
            result = await session.execute(select(self.model))
            return list(result.scalars().all())

    async def find(
        self,
        *,
        where: dict[str, Any] | None = None,
        skip: int | None = None,
        take: int | None = None,
        order_by: str | tuple[str, str] | list[str | tuple[str, str]] | None = None,
    ) -> list[Any]:
        statement = select(self.model)
        if where:
            statement = statement.where(*self._filters(where))
        if order_by:
            order_expressions = self._order_by(order_by)
            if isinstance(order_expressions, list):
                statement = statement.order_by(*order_expressions)
            else:
                statement = statement.order_by(order_expressions)
        if skip is not None:
            statement = statement.offset(skip)
        if take is not None:
            statement = statement.limit(take)
        async with self._session() as session:
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def find_and_count(
        self,
        *,
        where: dict[str, Any] | None = None,
        skip: int | None = None,
        take: int | None = None,
        order_by: str | tuple[str, str] | list[str | tuple[str, str]] | None = None,
    ) -> tuple[list[Any], int]:
        rows = await self.find(where=where, skip=skip, take=take, order_by=order_by)
        total = await self.count_where(where or {})
        return rows, total

    async def find_by(self, **criteria: Any) -> list[Any]:
        async with self._session() as session:
            result = await session.execute(select(self.model).where(*self._filters(criteria)))
            return list(result.scalars().all())

    async def find_one(self, primary_key: Any) -> Any | None:
        async with self._session() as session:
            return await session.get(self.model, primary_key)

    async def find_one_where(
        self,
        where: dict[str, Any],
        *,
        order_by: str | tuple[str, str] | list[str | tuple[str, str]] | None = None,
    ) -> Any | None:
        statement = select(self.model).where(*self._filters(where)).limit(1)
        if order_by:
            order_expressions = self._order_by(order_by)
            if isinstance(order_expressions, list):
                statement = statement.order_by(*order_expressions)
            else:
                statement = statement.order_by(order_expressions)
        async with self._session() as session:
            result = await session.execute(statement)
            return result.scalars().first()

    async def find_one_by(self, **criteria: Any) -> Any | None:
        async with self._session() as session:
            result = await session.execute(select(self.model).where(*self._filters(criteria)).limit(1))
            return result.scalars().first()

    async def find_one_or_fail_by(self, **criteria: Any) -> Any:
        entity = await self.find_one_by(**criteria)
        if entity is None:
            raise LookupError(f"{self.model.__name__} not found for criteria {criteria!r}")
        return entity

    async def find_one_or_fail(self, primary_key: Any) -> Any:
        entity = await self.find_one(primary_key)
        if entity is None:
            raise LookupError(f"{self.model.__name__} not found for primary key {primary_key!r}")
        return entity

    async def exists(self, **criteria: Any) -> bool:
        return await self.count(**criteria) > 0

    async def count(self, **criteria: Any) -> int:
        return await self.count_where(criteria)

    async def count_where(self, where: dict[str, Any] | None = None) -> int:
        async with self._session() as session:
            statement = select(func.count()).select_from(self.model)
            if where:
                statement = statement.where(*self._filters(where))
            result = await session.execute(statement)
            return int(result.scalar_one())

    async def save(self, entity: Any) -> Any:
        async with self._session(write=True) as session:
            session.add(entity)
            await self._commit_or_flush(session)
            await session.refresh(entity)
            return entity

    def create(self, values: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        return self.model(**{**(values or {}), **kwargs})

    def create_many(self, values: list[dict[str, Any]]) -> list[Any]:
        return [self.create(item) for item in values]

    def merge(self, entity: Any, values: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        for key, value in {**(values or {}), **kwargs}.items():
            setattr(entity, key, value)
        return entity

    async def insert_many(self, entities: list[Any]) -> list[Any]:
        async with self._session(write=True) as session:
            session.add_all(entities)
            await self._commit_or_flush(session)
            for entity in entities:
                await session.refresh(entity)
            return entities

    async def save_many(self, entities: list[Any]) -> list[Any]:
        return await self.insert_many(entities)

    async def upsert(self, criteria: dict[str, Any], values: dict[str, Any]) -> Any:
        entity = await self.find_one_by(**criteria)
        if entity is None:
            payload = {**criteria, **values}
            entity = self.model(**payload)
            return await self.save(entity)
        async with self._session(write=True) as session:
            for key, value in values.items():
                setattr(entity, key, value)
            session.add(entity)
            await self._commit_or_flush(session)
            await session.refresh(entity)
            return entity

    async def update(self, criteria: dict[str, Any], values: dict[str, Any]) -> int:
        async with self._session(write=True) as session:
            result = await session.execute(
                sqlalchemy_update(self.model).where(*self._filters(criteria)).values(**values)
            )
            await self._commit_or_flush(session)
            return int(getattr(result, "rowcount", 0) or 0)

    async def delete(self, entity: Any) -> None:
        async with self._session(write=True) as session:
            await session.delete(entity)
            await self._commit_or_flush(session)

    async def remove(self, entity: Any) -> Any:
        await self.delete(entity)
        return entity

    async def delete_by(self, **criteria: Any) -> int:
        async with self._session(write=True) as session:
            result = await session.execute(sqlalchemy_delete(self.model).where(*self._filters(criteria)))
            await self._commit_or_flush(session)
            return int(getattr(result, "rowcount", 0) or 0)

    async def clear(self) -> int:
        async with self._session(write=True) as session:
            result = await session.execute(sqlalchemy_delete(self.model))
            await self._commit_or_flush(session)
            return int(getattr(result, "rowcount", 0) or 0)

    def _filters(self, criteria: dict[str, Any]) -> list[Any]:
        return [self._filter_expression(key, value) for key, value in criteria.items()]

    def _filter_expression(self, key: str, value: Any) -> Any:
        if key in {"$or", "or"}:
            return or_(*[and_(*self._filters(item)) for item in value])
        if key in {"$and", "and"}:
            return and_(*[and_(*self._filters(item)) for item in value])
        column = getattr(self.model, key)
        if not isinstance(value, dict):
            return column == value
        expressions = []
        for operator, operand in value.items():
            if operator in {"$eq", "eq"}:
                expressions.append(column == operand)
            elif operator in {"$ne", "ne"}:
                expressions.append(column != operand)
            elif operator in {"$gt", "gt"}:
                expressions.append(column > operand)
            elif operator in {"$gte", "gte"}:
                expressions.append(column >= operand)
            elif operator in {"$lt", "lt"}:
                expressions.append(column < operand)
            elif operator in {"$lte", "lte"}:
                expressions.append(column <= operand)
            elif operator in {"$in", "in"}:
                expressions.append(column.in_(operand))
            elif operator in {"$nin", "nin"}:
                expressions.append(~column.in_(operand))
            elif operator in {"$like", "like"}:
                expressions.append(column.like(operand))
            elif operator in {"$ilike", "ilike"}:
                expressions.append(column.ilike(operand))
            elif operator in {"$isnull", "isnull"}:
                expressions.append(column.is_(None) if operand else column.is_not(None))
            elif operator in {"$between", "between"}:
                start, end = operand
                expressions.append(column.between(start, end))
            else:
                raise ValueError(f"Unsupported SQLAlchemy repository operator: {operator}")
        return expressions[0] if len(expressions) == 1 else and_(*expressions)

    def _order_by(self, order_by: str | tuple[str, str] | list[str | tuple[str, str]]) -> Any:
        if isinstance(order_by, list):
            return [self._order_by(item) for item in order_by]
        if isinstance(order_by, tuple):
            field, direction = order_by
        else:
            field, direction = order_by, "asc"
        column = getattr(self.model, field)
        return column.desc() if direction.lower() == "desc" else column.asc()

    @asynccontextmanager
    async def _session(self, *, write: bool = False) -> AsyncIterator[AsyncSession]:
        active_session = _current_session.get()
        if active_session is not None:
            yield active_session
            return
        async for session in self.service.session():
            yield session

    async def _commit_or_flush(self, session: AsyncSession) -> None:
        if _current_session.get() is session:
            await session.flush()
            return
        await session.commit()


def repository_token(model: type, data_source: str | None = None):
    _ensure_default_data_source(data_source)
    return token(f"SQLALCHEMY_REPOSITORY:{model.__module__}.{model.__name__}")


def get_repository_token(model: type, data_source: str | None = None):
    return repository_token(model, data_source=data_source)


def get_data_source_token(name: str | None = None) -> Any:
    _ensure_default_data_source(name)
    return SQLALCHEMY_DATA_SOURCE


def get_entity_manager_token(name: str | None = None) -> Any:
    _ensure_default_data_source(name)
    return SQLALCHEMY_ENTITY_MANAGER


def get_connection_token(name: str | None = None) -> Any:
    return get_data_source_token(name)


def InjectRepository(model: type, data_source: str | None = None):
    return Inject(repository_token(model, data_source=data_source))


def InjectEntityManager():
    return Inject(SQLALCHEMY_ENTITY_MANAGER)


def InjectDataSource(name: str | None = None):
    return Inject(get_data_source_token(name))


def InjectConnection(name: str | None = None):
    return InjectDataSource(name)


def Transactional(service_attr: str = "db"):
    def decorator(handler):
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            signature = None
        accepts_session = signature is not None and (
            "session" in signature.parameters
            or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        )

        @wraps(handler)
        async def wrapper(self, *args, **kwargs):
            service = getattr(self, service_attr)
            async with service.transaction() as session:
                if accepts_session:
                    kwargs.setdefault("session", session)
                return await handler(self, *args, **kwargs)

        return wrapper

    return decorator


Transaction = Transactional
InjectManager = InjectEntityManager


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
    def for_root(*, database_url: str, echo: bool = False, is_global: bool = True) -> type:
        cache_key = (database_url, echo, is_global)
        if cache_key in _root_module_cache:
            return _root_module_cache[cache_key]
        options = {"database_url": database_url, "echo": echo}

        @Module(
            providers=[
                use_value(SQLALCHEMY_OPTIONS, options),
                SqlAlchemyService,
                provider_factory(SQLALCHEMY_DATA_SOURCE, lambda service: service, inject=[SqlAlchemyService]),
                provider_factory(SQLALCHEMY_ENTITY_MANAGER, lambda service: service, inject=[SqlAlchemyService]),
            ],
            exports=[SqlAlchemyService, SQLALCHEMY_DATA_SOURCE, SQLALCHEMY_ENTITY_MANAGER],
            global_module=is_global,
        )
        class DynamicSqlAlchemyModule:
            pass

        _root_module_cache[cache_key] = DynamicSqlAlchemyModule
        return DynamicSqlAlchemyModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]],
        inject: list[Any] | None = None,
        is_global: bool = True,
    ) -> type:
        cache_key = (id(use_factory), tuple(inject or []), is_global)
        if cache_key in _async_root_module_cache:
            return _async_root_module_cache[cache_key]

        @Module(
            providers=[
                provider_factory(SQLALCHEMY_OPTIONS, use_factory, inject=inject or []),
                SqlAlchemyService,
                provider_factory(SQLALCHEMY_DATA_SOURCE, lambda service: service, inject=[SqlAlchemyService]),
                provider_factory(SQLALCHEMY_ENTITY_MANAGER, lambda service: service, inject=[SqlAlchemyService]),
            ],
            exports=[SqlAlchemyService, SQLALCHEMY_DATA_SOURCE, SQLALCHEMY_ENTITY_MANAGER],
            global_module=is_global,
        )
        class DynamicSqlAlchemyModule:
            pass

        _async_root_module_cache[cache_key] = DynamicSqlAlchemyModule
        return DynamicSqlAlchemyModule

    @staticmethod
    def for_feature(models: list[type] | tuple[type, ...]) -> type:
        cache_key = tuple(models)
        if cache_key in _feature_module_cache:
            return _feature_module_cache[cache_key]
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

        _feature_module_cache[cache_key] = DynamicSqlAlchemyFeatureModule
        return DynamicSqlAlchemyFeatureModule


TypeOrmModule = SqlAlchemyModule


class _UnsupportedOrmModule:
    recipe_name = "ORM"
    python_equivalent = "SqlAlchemyModule / TypeOrmModule"

    @classmethod
    def _raise(cls) -> NoReturn:
        raise UnsupportedDatabaseRecipeError(
            f"{cls.recipe_name} is a NestJS JavaScript ORM recipe and is not implemented by FaNest. "
            f"Use the Python-native {cls.python_equivalent} instead."
        )

    @classmethod
    def for_root(cls, *args: Any, **kwargs: Any) -> NoReturn:
        cls._raise()

    @classmethod
    def for_root_async(cls, *args: Any, **kwargs: Any) -> NoReturn:
        cls._raise()

    @classmethod
    def for_feature(cls, *args: Any, **kwargs: Any) -> NoReturn:
        cls._raise()


class SequelizeModule(_UnsupportedOrmModule):
    recipe_name = "SequelizeModule"


class MikroOrmModule(_UnsupportedOrmModule):
    recipe_name = "MikroOrmModule"


class PrismaModule(_UnsupportedOrmModule):
    recipe_name = "PrismaModule"
