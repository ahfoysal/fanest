from fastapi.testclient import TestClient

from fanest import (
    Controller,
    DiscoveryService,
    FaNestFactory,
    Get,
    Inject,
    Injectable,
    Module,
    SetMetadata,
    token,
    use_value,
)


@Injectable()
class DiscoverableService:
    pass


@Controller("discovery")
class DiscoveryController:
    def __init__(self, discovery: DiscoveryService):
        self.discovery = discovery

    @Get("/")
    async def index(self):
        providers = [item.token.__name__ for item in self.discovery.get_providers()]
        controllers = [controller.__name__ for controller in self.discovery.get_controllers()]
        return {"providers": providers, "controllers": controllers}


@Module(controllers=[DiscoveryController], providers=[DiscoverableService])
class DiscoveryModule:
    pass


def test_discovery_service_lists_registered_providers_and_controllers():
    response = TestClient(FaNestFactory.create(DiscoveryModule)).get("/discovery")

    assert response.json() == {
        "providers": ["DiscoverableService"],
        "controllers": ["DiscoveryController"],
    }


DISCOVERY_VALUE = token("DISCOVERY_VALUE")


class ModuleScopedDiscoverableService:
    def __init__(self, value: str = Inject(DISCOVERY_VALUE)):
        self.value = value


@Module(
    providers=[
        use_value(DISCOVERY_VALUE, "first"),
        ModuleScopedDiscoverableService,
    ]
)
class FirstDiscoveryFeatureModule:
    pass


@Module(
    providers=[
        use_value(DISCOVERY_VALUE, "second"),
        ModuleScopedDiscoverableService,
    ]
)
class SecondDiscoveryFeatureModule:
    pass


@Controller("discovery-context")
class DiscoveryContextController:
    def __init__(self, discovery: DiscoveryService):
        self.discovery = discovery

    @Get("/")
    async def index(self):
        providers = {}
        for item in self.discovery.get_providers():
            if item.token is not ModuleScopedDiscoverableService:
                continue
            assert item.module_type is not None
            providers[item.module_type.__name__] = item.instance.value
        return providers


@Module(
    imports=[FirstDiscoveryFeatureModule, SecondDiscoveryFeatureModule],
    controllers=[DiscoveryContextController],
)
class DiscoveryContextRootModule:
    pass


def test_discovery_service_resolves_providers_in_their_module_context():
    response = TestClient(FaNestFactory.create(DiscoveryContextRootModule)).get("/discovery-context")

    assert response.json() == {
        "FirstDiscoveryFeatureModule": "first",
        "SecondDiscoveryFeatureModule": "second",
    }


@SetMetadata("discovery:role", "worker")
@Injectable()
class MetadataDiscoverableService:
    pass


@Controller("discovery-metadata")
class DiscoveryMetadataController:
    def __init__(self, discovery: DiscoveryService):
        self.discovery = discovery

    @Get("/")
    async def index(self):
        [provider] = self.discovery.with_metadata("discovery:role")
        return {
            "token": provider.token.__name__,
            "metatype": provider.metatype.__name__,
            "metadata": provider.metadata,
        }


@Module(controllers=[DiscoveryMetadataController], providers=[MetadataDiscoverableService])
class DiscoveryMetadataModule:
    pass


def test_discovery_service_exposes_provider_metadata_wrappers():
    response = TestClient(FaNestFactory.create(DiscoveryMetadataModule)).get("/discovery-metadata")

    assert response.json() == {
        "token": "MetadataDiscoverableService",
        "metatype": "MetadataDiscoverableService",
        "metadata": {"discovery:role": "worker"},
    }
