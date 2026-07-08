import pytest

from fanest import (
    Controller,
    FaNestFactory,
    Get,
    Inject,
    Injectable,
    Module,
    forward_ref,
    token,
    use_existing,
    use_factory,
    use_value,
)


@Injectable()
class PrivateService:
    pass


@Module(providers=[PrivateService], exports=[])
class PrivateModule:
    pass


@Controller("leak")
class LeakyController:
    def __init__(self, private_service: PrivateService):
        self.private_service = private_service

    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(imports=[PrivateModule], controllers=[LeakyController])
class LeakyModule:
    pass


def test_imported_private_provider_cannot_leak_across_module_boundary():
    with pytest.raises(TypeError, match="not local or exported"):
        FaNestFactory.create(LeakyModule)


PRIVATE_TOKEN = token("PRIVATE_TOKEN")
ALIAS_TOKEN = token("ALIAS_TOKEN")
FACTORY_TOKEN = token("FACTORY_TOKEN")


@Module(providers=[use_factory(PRIVATE_TOKEN, lambda: "private")], exports=[])
class PrivateTokenModule:
    pass


class ExplicitInjectConsumer:
    def __init__(self, value: str = Inject(PRIVATE_TOKEN)):
        self.value = value


@Module(imports=[PrivateTokenModule], providers=[ExplicitInjectConsumer])
class ExplicitInjectLeakModule:
    pass


@Module(
    imports=[PrivateTokenModule],
    providers=[use_factory(FACTORY_TOKEN, lambda value: value, inject=[PRIVATE_TOKEN])],
)
class FactoryLeakModule:
    pass


@Module(
    imports=[PrivateTokenModule],
    providers=[use_existing(ALIAS_TOKEN, PRIVATE_TOKEN)],
)
class ExistingLeakModule:
    pass


def test_boundary_validation_covers_explicit_inject_factory_and_existing_providers():
    for module in [ExplicitInjectLeakModule, FactoryLeakModule, ExistingLeakModule]:
        with pytest.raises(TypeError, match="not local or exported"):
            FaNestFactory.create(module)


@Injectable()
class ForwardImportedService:
    def message(self):
        return "ok"


@Module(providers=[ForwardImportedService], exports=[ForwardImportedService])
class ForwardImportedModule:
    pass


@Controller("forward-import")
class ForwardImportController:
    def __init__(self, service: ForwardImportedService):
        self.service = service

    @Get("/")
    async def index(self):
        return {"message": self.service.message()}


@Module(imports=[forward_ref(lambda: ForwardImportedModule)], controllers=[ForwardImportController])
class ForwardImportAppModule:
    pass


def test_module_imports_can_use_forward_ref():
    app = FaNestFactory.create(ForwardImportAppModule)

    assert app.state.fanest_container.resolve(ForwardImportedService).message() == "ok"


SCOPED_MESSAGE = token("SCOPED_MESSAGE")


class ScopedMessageService:
    def __init__(self, message: str = Inject(SCOPED_MESSAGE)):
        self.message = message


@Controller("first-scope")
class FirstScopedController:
    def __init__(self, service: ScopedMessageService):
        self.service = service

    @Get("/")
    async def index(self):
        return {"message": self.service.message}


@Controller("second-scope")
class SecondScopedController:
    def __init__(self, service: ScopedMessageService):
        self.service = service

    @Get("/")
    async def index(self):
        return {"message": self.service.message}


@Module(
    controllers=[FirstScopedController],
    providers=[use_value(SCOPED_MESSAGE, "first"), ScopedMessageService],
)
class FirstScopedModule:
    pass


@Module(
    controllers=[SecondScopedController],
    providers=[use_value(SCOPED_MESSAGE, "second"), ScopedMessageService],
)
class SecondScopedModule:
    pass


@Module(imports=[FirstScopedModule, SecondScopedModule])
class ScopedRootModule:
    pass


def test_sibling_modules_can_use_same_provider_token_without_clobbering():
    from fastapi.testclient import TestClient

    client = TestClient(FaNestFactory.create(ScopedRootModule))

    assert client.get("/first-scope").json() == {"message": "first"}
    assert client.get("/second-scope").json() == {"message": "second"}


EXPORTED_SCOPED_MESSAGE = token("EXPORTED_SCOPED_MESSAGE")


class ExportedScopedService:
    def __init__(self, message: str = Inject(SCOPED_MESSAGE)):
        self.message = message


@Module(
    providers=[use_value(SCOPED_MESSAGE, "export-owner"), ExportedScopedService],
    exports=[ExportedScopedService],
)
class ExportOwnerModule:
    pass


@Controller("exported-scope")
class ExportConsumerController:
    def __init__(self, service: ExportedScopedService):
        self.service = service

    @Get("/")
    async def index(self):
        return {"message": self.service.message}


@Module(
    imports=[ExportOwnerModule],
    controllers=[ExportConsumerController],
    providers=[use_value(SCOPED_MESSAGE, "consumer-local")],
)
class ExportConsumerModule:
    pass


def test_exported_provider_resolves_its_own_module_local_dependencies():
    from fastapi.testclient import TestClient

    client = TestClient(FaNestFactory.create(ExportConsumerModule))

    assert client.get("/exported-scope").json() == {"message": "export-owner"}
