from fastapi.testclient import TestClient

from fanest import FaNestFactory, Injectable, Module, token, use_existing


events: list[str] = []


@Injectable()
class FirstLifecycleService:
    async def on_module_init(self):
        events.append("first:init")

    async def on_application_bootstrap(self):
        events.append("first:bootstrap")

    async def before_application_shutdown(self):
        events.append("first:before_shutdown")

    async def on_module_destroy(self):
        events.append("first:destroy")

    async def on_application_shutdown(self):
        events.append("first:shutdown")


@Injectable()
class SecondLifecycleService:
    async def on_module_init(self):
        events.append("second:init")

    async def on_application_bootstrap(self):
        events.append("second:bootstrap")

    async def before_application_shutdown(self):
        events.append("second:before_shutdown")

    async def on_module_destroy(self):
        events.append("second:destroy")

    async def on_application_shutdown(self):
        events.append("second:shutdown")


@Module(providers=[FirstLifecycleService, SecondLifecycleService])
class LifecycleModule:
    pass


LIFECYCLE_ALIAS = token("LIFECYCLE_ALIAS")


@Module(providers=[FirstLifecycleService, use_existing(LIFECYCLE_ALIAS, FirstLifecycleService)])
class LifecycleAliasModule:
    pass


def test_lifecycle_hooks_run_in_nest_style_order():
    events.clear()

    with TestClient(FaNestFactory.create(LifecycleModule)):
        assert events == [
            "first:init",
            "second:init",
            "first:bootstrap",
            "second:bootstrap",
        ]

    assert events == [
        "first:init",
        "second:init",
        "first:bootstrap",
        "second:bootstrap",
        "second:before_shutdown",
        "first:before_shutdown",
        "second:destroy",
        "first:destroy",
        "second:shutdown",
        "first:shutdown",
    ]


def test_use_existing_alias_does_not_duplicate_lifecycle_hooks():
    events.clear()

    with TestClient(FaNestFactory.create(LifecycleAliasModule)):
        assert events == ["first:init", "first:bootstrap"]

    assert events == [
        "first:init",
        "first:bootstrap",
        "first:before_shutdown",
        "first:destroy",
        "first:shutdown",
    ]


@Injectable()
class ImportedLifecycleService:
    async def on_module_init(self):
        events.append("imported:init")

    async def on_application_bootstrap(self):
        events.append("imported:bootstrap")


@Injectable()
class RootLifecycleService:
    def __init__(self, imported: ImportedLifecycleService):
        self.imported = imported

    async def on_module_init(self):
        events.append("root:init")

    async def on_application_bootstrap(self):
        events.append("root:bootstrap")


@Module(providers=[ImportedLifecycleService], exports=[ImportedLifecycleService])
class ImportedLifecycleModule:
    pass


@Module(imports=[ImportedLifecycleModule], providers=[RootLifecycleService])
class RootLifecycleModule:
    pass


def test_lifecycle_hooks_bootstrap_imported_modules_before_dependents():
    events.clear()

    with TestClient(FaNestFactory.create(RootLifecycleModule)):
        assert events == [
            "imported:init",
            "root:init",
            "imported:bootstrap",
            "root:bootstrap",
        ]


def test_standalone_application_context_di_and_lifecycle():
    """FaNestFactory.create_application_context builds a non-HTTP app with DI +
    lifecycle, resolvable via get()/resolve() and closable (NestJS
    createApplicationContext parity)."""
    import asyncio

    from fanest import FaNestApplicationContext

    log: list[str] = []

    @Injectable()
    class Repository:
        def all(self):
            return [1, 2, 3]

    @Injectable()
    class ReportService:
        def __init__(self, repository: Repository):
            self.repository = repository

        async def on_module_init(self):
            log.append("init")

        async def on_application_bootstrap(self):
            log.append("bootstrap")

        async def on_application_shutdown(self):
            log.append("shutdown")

        def total(self):
            return sum(self.repository.all())

    @Module(providers=[Repository, ReportService], exports=[ReportService])
    class ContextModule:
        pass

    async def scenario():
        context = await FaNestFactory.create_application_context(ContextModule)
        assert isinstance(context, FaNestApplicationContext)
        service = context.get(ReportService)
        assert service.total() == 6
        assert isinstance(service.repository, Repository)
        assert (await context.resolve(Repository)).all() == [1, 2, 3]
        init_log = list(log)
        await context.close()
        await context.close()  # idempotent
        return init_log, list(log)

    init_log, final_log = asyncio.run(scenario())
    assert init_log == ["init", "bootstrap"]
    assert final_log == ["init", "bootstrap", "shutdown"]


def test_standalone_application_context_async_context_manager():
    import asyncio

    @Injectable()
    class Counter:
        value = 0

        def bump(self):
            Counter.value += 1
            return Counter.value

    @Module(providers=[Counter], exports=[Counter])
    class CounterModule:
        pass

    async def scenario():
        async with await FaNestFactory.create_application_context(CounterModule) as context:
            return context.get(Counter).bump()

    assert asyncio.run(scenario()) == 1
