import inspect
from typing import Any, Callable

from fanest import Controller, Get, Injectable, Module, Optional, use_value
from fanest.core.providers import token

HEALTH_INDICATORS = token("HEALTH_INDICATORS")


class HealthIndicator:
    def __init__(self, name: str, check: Callable[[], Any]) -> None:
        self.name = name
        self.check = check

    async def run(self) -> dict[str, Any]:
        result = self.check()
        if inspect.isawaitable(result):
            result = await result
        return {self.name: result}


@Injectable()
class HealthService:
    def __init__(self, indicators: list[HealthIndicator] | None = Optional(HEALTH_INDICATORS)):
        self.indicators = indicators or []

    async def check(self) -> dict[str, Any]:
        if not self.indicators:
            return {"status": "ok"}
        details: dict[str, Any] = {}
        status = "ok"
        for indicator in self.indicators:
            result = await indicator.run()
            details.update(result)
            if any(value.get("status") != "ok" for value in result.values() if isinstance(value, dict)):
                status = "error"
        return {"status": status, "details": details}


@Controller("health")
class HealthController:
    def __init__(self, health_service: HealthService):
        self.health_service = health_service

    @Get("/")
    async def check(self):
        return await self.health_service.check()


class HealthModule:
    @staticmethod
    def register(indicators: list[HealthIndicator] | None = None) -> type:
        @Module(
            controllers=[HealthController],
            providers=[use_value(HEALTH_INDICATORS, indicators or []), HealthService],
            exports=[HealthService],
        )
        class DynamicHealthModule:
            pass

        return DynamicHealthModule
