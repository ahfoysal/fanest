from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, LazyModuleLoader, Module, dynamic_module, use_value
from fanest.core.discovery import DiscoveryService


@Injectable()
class LazyPrivateService:
    value = "private"


@Injectable()
class LazyExportedService:
    value = "exported"


@Module(providers=[LazyPrivateService, LazyExportedService], exports=[LazyExportedService])
class LazyFeatureModule:
    pass


@Controller("lazy")
class LazyController:
    def __init__(self, loader: LazyModuleLoader, discovery: DiscoveryService):
        self.loader = loader
        self.discovery = discovery

    @Get("sync")
    def sync_load(self):
        module_ref = self.loader.load_sync(LazyFeatureModule)
        private = module_ref.get(LazyPrivateService, strict=True)
        exported = module_ref.get(LazyExportedService)
        discovered = [provider.token for provider in self.discovery.get_providers()]
        return {
            "private": private.value,
            "exported": exported.value,
            "discovered": LazyPrivateService in discovered,
        }

    @Get("async")
    async def async_load(self):
        module_ref = await self.loader.load(
            dynamic_module(
                LazyDynamicModule,
                providers=[use_value("lazy-message", "dynamic")],
                exports=["lazy-message"],
            )
        )
        return {"message": module_ref.get("lazy-message", strict=True)}


@Module()
class LazyDynamicModule:
    pass


@Module(controllers=[LazyController])
class LazyRootModule:
    pass


def test_lazy_module_loader_loads_modules_into_existing_container():
    client = TestClient(FaNestFactory.create(LazyRootModule))
    response = client.get("/lazy/sync")

    assert response.json() == {
        "private": "private",
        "exported": "exported",
        "discovered": True,
    }


def test_lazy_module_loader_supports_async_dynamic_modules():
    client = TestClient(FaNestFactory.create(LazyRootModule))
    response = client.get("/lazy/async")

    assert response.json() == {"message": "dynamic"}
