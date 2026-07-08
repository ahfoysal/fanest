from typing import Any

from fanest import Controller, Get, Injectable, Module


@Injectable()
class HealthService:
    def check(self) -> dict[str, Any]:
        return {"status": "ok"}


@Controller("health")
class HealthController:
    def __init__(self, health_service: HealthService):
        self.health_service = health_service

    @Get("/")
    async def check(self):
        return self.health_service.check()


class HealthModule:
    @staticmethod
    def register() -> type:
        @Module(controllers=[HealthController], providers=[HealthService], exports=[HealthService])
        class DynamicHealthModule:
            pass

        return DynamicHealthModule
