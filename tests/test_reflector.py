from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Reflector, SetMetadata, UseGuards


class RolesGuard:
    def __init__(self, reflector: Reflector):
        self.reflector = reflector

    def can_activate(self, context):
        roles = self.reflector.get_all_and_override(
            "roles",
            [context.handler, context.controller.__class__],
            default=[],
        )
        return "admin" in roles


@Controller("reflector")
@UseGuards(RolesGuard)
class ReflectorController:
    @SetMetadata("roles", ["admin"])
    @Get("/")
    async def index(self):
        return {"ok": True}

    @SetMetadata("roles", ["user"])
    @Get("/blocked")
    async def blocked(self):
        return {"ok": False}


@Module(controllers=[ReflectorController], providers=[RolesGuard])
class ReflectorModule:
    pass


def test_reflector_reads_handler_metadata_for_guards():
    client = TestClient(FaNestFactory.create(ReflectorModule))

    assert client.get("/reflector").status_code == 200
    assert client.get("/reflector/blocked").status_code == 403
