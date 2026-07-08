from fastapi.testclient import TestClient

from fanest import Controller, DiscoveryService, FaNestFactory, Get, Injectable, Module


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
