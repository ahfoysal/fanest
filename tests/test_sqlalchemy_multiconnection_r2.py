"""Round-2 regressions for SQLAlchemy multi-connection isolation and migrations."""

import pytest

from fanest import FaNestFactory, Module
from fanest.sqlalchemy.module import (
    MigrationManager,
    SqlAlchemyModule,
    SqlAlchemyService,
    get_current_session,
    get_data_source_token,
)


@pytest.mark.anyio
async def test_transactions_on_different_connections_do_not_leak():
    db_a = SqlAlchemyService({"database_url": "sqlite+aiosqlite:///:memory:"})
    db_b = SqlAlchemyService({"database_url": "sqlite+aiosqlite:///:memory:"})
    try:
        async with db_a.transaction() as session_a:
            # A's session is registered against A only; B has no active session.
            assert get_current_session(db_a) is session_a
            assert get_current_session(db_b) is None
            async with db_b.transaction() as session_b:
                assert session_b is not session_a
                assert get_current_session(db_b) is session_b
                # Entering B's transaction must not hijack A's session.
                assert get_current_session(db_a) is session_a
            # After B exits, A is still active and B is clear again.
            assert get_current_session(db_a) is session_a
            assert get_current_session(db_b) is None
    finally:
        await db_a.close()
        await db_b.close()


def test_two_unnamed_for_root_connections_fail_loudly(tmp_path):
    mod_a = SqlAlchemyModule.for_root(database_url="sqlite+aiosqlite:///:memory:")
    mod_b = SqlAlchemyModule.for_root(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'second.db'}"
    )

    @Module(imports=[mod_a, mod_b])
    class TwoDefaultsApp:
        pass

    app = FaNestFactory.create(TwoDefaultsApp)
    with pytest.raises(RuntimeError, match="unnamed"):
        app.state.fanest_container.resolve(SqlAlchemyService)


def test_named_connection_coexists_with_default_and_is_distinct(tmp_path):
    default_mod = SqlAlchemyModule.for_root(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}"
    )
    analytics_mod = SqlAlchemyModule.for_root(
        name="analytics",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'analytics.db'}",
    )

    @Module(imports=[default_mod, analytics_mod])
    class MultiApp:
        pass

    container = FaNestFactory.create(MultiApp).state.fanest_container
    default_service = container.resolve(get_data_source_token())
    analytics_service = container.resolve(get_data_source_token("analytics"))

    # The named connection is a distinct service bound to a distinct engine,
    # not a silent alias of the default (the bug).
    assert default_service is not analytics_service
    assert str(default_service.engine.url) != str(analytics_service.engine.url)
    assert "analytics.db" in str(analytics_service.engine.url)


def test_migration_numbering_uses_max_prefix_plus_one(tmp_path):
    manager = MigrationManager(tmp_path)
    first = manager.create("add users")
    manager.create("add orders")
    third = manager.create("add invoices")
    assert first.name.startswith("0001_")
    assert third.name.startswith("0003_")

    first.unlink()  # delete the earliest migration
    # Next number is max(existing)+1 = 0004, not a duplicate 0002 that would
    # sort ahead of the surviving migrations.
    fourth = manager.create("add payments")
    assert fourth.name.startswith("0004_")

    numbers = [path.name.split("_", 1)[0] for path in tmp_path.glob("*.py")]
    assert len(numbers) == len(set(numbers))  # no duplicate sequence numbers
