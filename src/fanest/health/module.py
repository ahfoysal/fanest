import inspect
import resource
import shutil
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


class DiskHealthIndicator(HealthIndicator):
    def __init__(self, name: str = "disk", *, path: str = ".", threshold_percent: float = 90.0) -> None:
        self.path = path
        self.threshold_percent = threshold_percent
        super().__init__(name, self._check)

    def _check(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.path)
        used_percent = (usage.used / usage.total) * 100 if usage.total else 0
        return {
            "status": "ok" if used_percent <= self.threshold_percent else "error",
            "path": self.path,
            "used_percent": round(used_percent, 2),
            "threshold_percent": self.threshold_percent,
        }


class MemoryHealthIndicator(HealthIndicator):
    def __init__(
        self,
        name: str = "memory",
        *,
        heap_threshold_mb: float | None = None,
        rss_threshold_mb: float | None = None,
    ) -> None:
        self.heap_threshold_mb = heap_threshold_mb
        self.rss_threshold_mb = rss_threshold_mb
        super().__init__(name, self._check)

    def _check(self) -> dict[str, Any]:
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        thresholds = [value for value in [self.heap_threshold_mb, self.rss_threshold_mb] if value is not None]
        status = "ok" if not thresholds or all(rss_mb <= threshold for threshold in thresholds) else "error"
        return {
            "status": status,
            "rss_mb": round(rss_mb, 2),
            "heap_threshold_mb": self.heap_threshold_mb,
            "rss_threshold_mb": self.rss_threshold_mb,
        }


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
    def register(indicators: list[HealthIndicator] | None = None, *, is_global: bool = False) -> type:
        @Module(
            controllers=[HealthController],
            providers=[use_value(HEALTH_INDICATORS, indicators or []), HealthService],
            exports=[HealthService],
            global_module=is_global,
        )
        class DynamicHealthModule:
            pass

        return DynamicHealthModule
