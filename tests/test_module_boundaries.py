import pytest

from fanest import Controller, FaNestFactory, Get, Injectable, Module


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
