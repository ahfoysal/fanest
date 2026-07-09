import pytest

from fanest import (
    Controller,
    FaNestFactory,
    Get,
    Inject,
    Injectable,
    Module,
    ModuleRef,
    Query,
    Self,
    SkipSelf,
    UseGuards,
    forward_ref,
    token,
    use_existing,
    use_factory,
    use_value,
)
from fanest.core.module_ref import UnknownProviderError


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

    assert app.state.fanest_container.resolve(
        ForwardImportedService,
        module_key=ForwardImportedModule,
    ).message() == "ok"


@Injectable()
class StringForwardRefServiceA:
    def __init__(self, service_b: "StringForwardRefServiceB" = Inject(forward_ref(lambda: "StringForwardRefServiceB"))):
        self.service_b = service_b

    def name(self):
        return "a"

    def peer_name(self):
        return self.service_b.name()


@Injectable()
class StringForwardRefServiceB:
    def __init__(self, service_a: "StringForwardRefServiceA" = Inject(forward_ref(lambda: "StringForwardRefServiceA"))):
        self.service_a = service_a

    def name(self):
        return "b"

    def peer_name(self):
        return self.service_a.name()


@Module(
    imports=[forward_ref(lambda: StringForwardRefModuleB)],
    providers=[StringForwardRefServiceA],
    exports=[StringForwardRefServiceA],
)
class StringForwardRefModuleA:
    pass


@Module(
    imports=[forward_ref(lambda: StringForwardRefModuleA)],
    providers=[StringForwardRefServiceB],
    exports=[StringForwardRefServiceB],
)
class StringForwardRefModuleB:
    pass


@Module(imports=[StringForwardRefModuleA, StringForwardRefModuleB])
class StringForwardRefAppModule:
    pass


def test_circular_modules_support_string_forward_ref_dependencies():
    app = FaNestFactory.create(StringForwardRefAppModule)
    container = app.state.fanest_container

    service_a = container.resolve(StringForwardRefServiceA, module_key=StringForwardRefModuleA)
    service_b = container.resolve(StringForwardRefServiceB, module_key=StringForwardRefModuleB)

    assert service_a.peer_name() == "b"
    assert service_b.peer_name() == "a"


@Injectable()
class AnnotationForwardRefAuthService:
    def __init__(self, user_service: forward_ref(lambda: "AnnotationForwardRefUserService")):
        self.user_service = user_service

    def validate(self):
        return self.user_service.name()


@Injectable()
class AnnotationForwardRefUserService:
    def __init__(self, auth_service: AnnotationForwardRefAuthService):
        self.auth_service = auth_service

    def name(self):
        return "user"


@Module(providers=[AnnotationForwardRefAuthService, AnnotationForwardRefUserService])
class AnnotationForwardRefModule:
    pass


def test_forward_ref_annotations_create_lazy_proxy_for_constructor_cycles():
    app = FaNestFactory.create(AnnotationForwardRefModule)
    service = app.state.fanest_container.resolve(
        AnnotationForwardRefAuthService,
        module_key=AnnotationForwardRefModule,
    )

    assert service.validate() == "user"


@Injectable()
class PrivateGlobalLookupService:
    pass


@Module(providers=[PrivateGlobalLookupService], exports=[])
class PrivateGlobalLookupModule:
    pass


@Module(imports=[PrivateGlobalLookupModule])
class PrivateGlobalLookupRootModule:
    pass


def test_private_module_providers_do_not_resolve_from_global_container_lookup():
    app = FaNestFactory.create(PrivateGlobalLookupRootModule)

    with pytest.raises(KeyError):
        app.state.fanest_container.resolve(PrivateGlobalLookupService)


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


class MissingDatabaseConnection:
    pass


class MissingDatabaseConsumer:
    def __init__(self, connection: MissingDatabaseConnection):
        self.connection = connection


@Module(providers=[MissingDatabaseConsumer])
class MissingProviderModule:
    pass


def test_unregistered_class_dependency_fails_startup_instead_of_auto_instantiating():
    with pytest.raises(TypeError, match="not local or exported"):
        FaNestFactory.create(MissingProviderModule)


class RootMissingService:
    pass


def test_container_does_not_silently_instantiate_unregistered_class():
    app = FaNestFactory.create(ForwardImportAppModule)

    with pytest.raises(KeyError):
        app.state.fanest_container.resolve(RootMissingService)


def test_module_ref_get_reports_unregistered_class_as_unknown_provider():
    app = FaNestFactory.create(ForwardImportAppModule)
    module_ref = app.state.fanest_container.resolve(ModuleRef)

    with pytest.raises(UnknownProviderError):
        module_ref.get(RootMissingService)


def test_local_string_annotations_resolve_against_module_providers_without_type_hint_crash():
    @Injectable()
    class LocalStringServiceB:
        def name(self):
            return "b"

    @Injectable()
    class LocalStringServiceA:
        def __init__(self, service_b: "LocalStringServiceB"):
            self.service_b = service_b

    @Module(providers=[LocalStringServiceA, LocalStringServiceB])
    class LocalStringModule:
        pass

    app = FaNestFactory.create(LocalStringModule)

    service = app.state.fanest_container.resolve(LocalStringServiceA, module_key=LocalStringModule)

    assert service.service_b.name() == "b"


def test_ambiguous_string_provider_names_fail_instead_of_resolving_random_class():
    duplicate_one = type(
        "DuplicateStringTokenService",
        (),
        {"__module__": __name__},
    )
    duplicate_two = type(
        "DuplicateStringTokenService",
        (),
        {"__module__": __name__},
    )

    class AmbiguousStringConsumer:
        def __init__(self, service=Inject("DuplicateStringTokenService")):
            self.service = service

    @Module(providers=[duplicate_one, duplicate_two, AmbiguousStringConsumer])
    class AmbiguousStringModule:
        pass

    with pytest.raises(TypeError, match="not local or exported"):
        FaNestFactory.create(AmbiguousStringModule)


PARENT_CHILD_TOKEN = token("PARENT_CHILD_TOKEN")


class SelfConsumer:
    def __init__(self, value: str = Self(PARENT_CHILD_TOKEN)):
        self.value = value


class SkipSelfConsumer:
    def __init__(self, value: str = SkipSelf(PARENT_CHILD_TOKEN)):
        self.value = value


@Module(providers=[use_value(PARENT_CHILD_TOKEN, "parent")], exports=[PARENT_CHILD_TOKEN])
class ParentTokenModule:
    pass


@Module(
    imports=[ParentTokenModule],
    providers=[
        use_value(PARENT_CHILD_TOKEN, "child"),
        SelfConsumer,
        SkipSelfConsumer,
    ],
)
class ChildTokenModule:
    pass


def test_self_and_skip_self_injection_respect_module_boundaries():
    app = FaNestFactory.create(ChildTokenModule)
    container = app.state.fanest_container

    assert container.resolve(SelfConsumer, module_key=ChildTokenModule).value == "child"
    assert container.resolve(SkipSelfConsumer, module_key=ChildTokenModule).value == "parent"


class SkipSelfMissingConsumer:
    def __init__(self, value: str = SkipSelf(PARENT_CHILD_TOKEN)):
        self.value = value


@Module(providers=[use_value(PARENT_CHILD_TOKEN, "child"), SkipSelfMissingConsumer])
class SkipSelfMissingModule:
    pass


def test_skip_self_injection_fails_when_token_is_only_local():
    with pytest.raises(TypeError, match="not local or exported"):
        FaNestFactory.create(SkipSelfMissingModule)


class InheritedGuard:
    def can_activate(self, context):
        return context.request.query_params.get("allow") == "yes"


class BaseInheritedController:
    @UseGuards(InheritedGuard)
    @Get("/")
    async def index(self, name: str = Query()):
        return {"name": name}


@Controller("inherited")
class InheritedController(BaseInheritedController):
    pass


@Module(controllers=[InheritedController])
class InheritedControllerModule:
    pass


def test_inherited_route_metadata_registers_implicit_enhancer_providers():
    from fastapi.testclient import TestClient

    client = TestClient(FaNestFactory.create(InheritedControllerModule))

    assert client.get("/inherited", params={"name": "ada"}).status_code == 403
    assert client.get("/inherited", params={"name": "ada", "allow": "yes"}).json() == {"name": "ada"}


class ReExportedService:
    def message(self):
        return "re-exported"


@Module(providers=[ReExportedService], exports=[ReExportedService])
class ReExportSourceModule:
    pass


@Module(imports=[ReExportSourceModule], exports=[ReExportSourceModule])
class ReExportBridgeModule:
    pass


class ReExportConsumer:
    def __init__(self, service: ReExportedService):
        self.service = service


@Module(imports=[ReExportBridgeModule], providers=[ReExportConsumer])
class ReExportConsumerModule:
    pass


def test_modules_can_re_export_imported_modules():
    app = FaNestFactory.create(ReExportConsumerModule)
    consumer = app.state.fanest_container.resolve(ReExportConsumer, module_key=ReExportConsumerModule)

    assert consumer.service.message() == "re-exported"


class UnknownExportService:
    pass


@Module(exports=[UnknownExportService])
class InvalidExportModule:
    pass


def test_module_export_must_be_local_or_re_exported_from_an_import():
    with pytest.raises(TypeError, match="exports .*not local or exported"):
        FaNestFactory.create(InvalidExportModule)


class ScanOnlyGuard:
    def can_activate(self, context):
        return True


@Controller("scan-isolation")
@UseGuards(ScanOnlyGuard)
class ScanIsolationController:
    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(controllers=[ScanIsolationController])
class ScanIsolationModule:
    pass


def test_implicit_provider_discovery_does_not_mutate_module_metadata_between_scans():
    metadata = getattr(ScanIsolationModule, "__fanest_module__")
    assert metadata.providers == []

    FaNestFactory.create(ScanIsolationModule)
    FaNestFactory.create(ScanIsolationModule)

    assert metadata.providers == []
