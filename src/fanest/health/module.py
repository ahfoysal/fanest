import asyncio
import inspect
import resource
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, cast

from fastapi.responses import JSONResponse

from fanest import Controller, Get, Injectable, Module, Optional, use_value
from fanest.core.providers import InjectMarker
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

HEALTH_INDICATORS = token("HEALTH_INDICATORS")
HEALTH_OPTIONS = token("HEALTH_OPTIONS")


@dataclass(frozen=True)
class HealthModuleOptions:
    error_status_code: int = 503
    timeout_seconds: float | None = None
    include_error_messages: bool = True


class HealthCheckError(Exception):
    def __init__(self, name: str, reason: str) -> None:
        super().__init__(reason)
        self.name = name
        self.reason = reason


class HealthIndicator:
    def __init__(self, name: str, check: Callable[[], Any], *, timeout_seconds: float | None = None) -> None:
        self.name = name
        self.check = check
        self.timeout_seconds = timeout_seconds

    async def run(self) -> dict[str, Any]:
        result = await self._run_check()
        return {self.name: self._normalize_result(result)}

    async def _run_check(self) -> Any:
        result = self.check()
        if inspect.isawaitable(result):
            if self.timeout_seconds is None:
                return await result
            return await asyncio.wait_for(result, timeout=self.timeout_seconds)
        return result

    def _normalize_result(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return {"status": result.get("status", "ok"), **result}
        if isinstance(result, bool):
            return {"status": "ok" if result else "error"}
        if result is None:
            return {"status": "ok"}
        return {"status": "ok", "value": result}


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
        rss_mb = self._rss_mb()
        thresholds = [value for value in [self.heap_threshold_mb, self.rss_threshold_mb] if value is not None]
        status = "ok" if not thresholds or all(rss_mb <= threshold for threshold in thresholds) else "error"
        return {
            "status": status,
            "rss_mb": round(rss_mb, 2),
            "heap_threshold_mb": self.heap_threshold_mb,
            "rss_threshold_mb": self.rss_threshold_mb,
        }

    def _rss_mb(self) -> float:
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return max_rss / 1024 / 1024
        return max_rss / 1024


@Injectable()
class HealthService:
    def __init__(
        self,
        indicators: list[HealthIndicator] | None = Optional(HEALTH_INDICATORS),
        options: HealthModuleOptions | None = Optional(HEALTH_OPTIONS),
    ):
        self.indicators = [] if isinstance(indicators, InjectMarker) else indicators or []
        self.options = HealthModuleOptions() if isinstance(options, InjectMarker) else options or HealthModuleOptions()

    async def check(self) -> dict[str, Any]:
        if not self.indicators:
            return {"status": "ok"}
        details: dict[str, Any] = {}
        status = "ok"
        results = await asyncio.gather(*(self._run_indicator(indicator) for indicator in self.indicators))
        for result in results:
            details.update(result)
            if any(value.get("status") != "ok" for value in result.values() if isinstance(value, dict)):
                status = "error"
        return {"status": status, "details": details}

    async def check_indicators(self, indicators: list[Callable[[], Any]]) -> dict[str, Any]:
        previous = self.indicators
        self.indicators = [
            item if isinstance(item, HealthIndicator) else HealthIndicator(getattr(item, "__name__", "indicator"), item)
            for item in indicators
        ]
        try:
            return await self.check()
        finally:
            self.indicators = previous

    def ping_check(self, name: str, value: Any = True) -> dict[str, Any]:
        status = "ok" if value else "error"
        return {name: {"status": status}}

    async def _run_indicator(self, indicator: HealthIndicator) -> dict[str, Any]:
        try:
            if self.options.timeout_seconds is None or indicator.timeout_seconds is not None:
                return await indicator.run()
            return await asyncio.wait_for(indicator.run(), timeout=self.options.timeout_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            return {indicator.name: self._error_result("timeout")}
        except HealthCheckError as exc:
            return {indicator.name: self._error_result(exc.reason)}
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}" if self.options.include_error_messages else type(exc).__name__
            return {indicator.name: self._error_result(reason)}

    def _error_result(self, reason: str) -> dict[str, Any]:
        payload = {"status": "error"}
        if self.options.include_error_messages:
            payload["error"] = reason
        return payload


@Controller("health")
class HealthController:
    def __init__(
        self,
        health_service: HealthService,
        options: HealthModuleOptions | None = Optional(HEALTH_OPTIONS),
    ):
        self.health_service = health_service
        self.options = HealthModuleOptions() if isinstance(options, InjectMarker) else options or HealthModuleOptions()

    @Get("/")
    async def check(self):
        result = await self.health_service.check()
        if result.get("status") == "error":
            return JSONResponse(status_code=self.options.error_status_code, content=result)
        return result


class HealthModule:
    @staticmethod
    def register(
        indicators: list[HealthIndicator] | None = None,
        *,
        error_status_code: int = 503,
        timeout_seconds: float | None = None,
        include_error_messages: bool = True,
        is_global: bool = False,
    ) -> type:
        options = HealthModuleOptions(
            error_status_code=error_status_code,
            timeout_seconds=timeout_seconds,
            include_error_messages=include_error_messages,
        )

        @Module(
            controllers=[HealthController],
            providers=[use_value(HEALTH_INDICATORS, indicators or []), use_value(HEALTH_OPTIONS, options), HealthService],
            exports=[HealthService, HEALTH_OPTIONS],
            global_module=is_global,
        )
        class DynamicHealthModule:
            pass

        return DynamicHealthModule

    @staticmethod
    def register_async(
        *,
        use_factory: Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]],
        inject: list[Any] | None = None,
        imports: list[Any] | None = None,
        is_global: bool = False,
    ) -> type:
        async def options_factory(*dependencies: Any) -> HealthModuleOptions:
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[Any], result)
            return HealthModuleOptions(
                error_status_code=result.get("error_status_code", 503),
                timeout_seconds=result.get("timeout_seconds"),
                include_error_messages=result.get("include_error_messages", True),
            )

        async def indicators_factory(*dependencies: Any) -> list[HealthIndicator]:
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[Any], result)
            return result.get("indicators", [])

        @Module(
            imports=imports or [],
            controllers=[HealthController],
            providers=[
                provider_factory(HEALTH_OPTIONS, options_factory, inject=inject or []),
                provider_factory(HEALTH_INDICATORS, indicators_factory, inject=inject or []),
                HealthService,
            ],
            exports=[HealthService, HEALTH_OPTIONS],
            global_module=is_global,
        )
        class DynamicHealthModule:
            pass

        return DynamicHealthModule
