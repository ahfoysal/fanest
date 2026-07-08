import asyncio
import inspect
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from croniter import croniter

from fanest.schedule.registry import SchedulerRegistry


class ScheduleRunner:
    def __init__(self, providers: Iterable[Any], registry: SchedulerRegistry | None = None) -> None:
        self.providers = providers
        self.tasks: list[asyncio.Task] = []
        self.registry = registry or SchedulerRegistry()

    def start(self) -> None:
        for provider in self.providers:
            for handler_name, handler in inspect.getmembers(provider, predicate=callable):
                metadata = getattr(handler, "__fanest_schedule__", None)
                if metadata is None:
                    continue
                name = metadata.get("name") or self._default_name(provider, handler_name, metadata["type"])
                if metadata["type"] == "interval":
                    self._schedule(name, metadata, self._run_interval(metadata, handler))
                elif metadata["type"] == "cron":
                    self._schedule(name, metadata, self._run_cron(metadata, handler))
                elif metadata["type"] == "timeout":
                    self._schedule(name, metadata, self._run_timeout(metadata, handler))
                else:
                    raise ValueError(f"Unknown schedule type: {metadata['type']}")

    async def stop(self) -> None:
        self.registry.clear()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    def _schedule(self, name: str, metadata: dict[str, Any], coroutine: Any) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        self.registry.add(name, metadata["type"], task, metadata)

    def _default_name(self, provider: Any, handler_name: str, kind: str) -> str:
        return f"{provider.__class__.__name__}.{handler_name}:{kind}"

    async def _run_interval(self, metadata: dict[str, Any], handler: Any) -> None:
        delay = float(metadata["seconds"])
        while True:
            await asyncio.sleep(delay)
            await self._call(handler)

    async def _run_timeout(self, metadata: dict[str, Any], handler: Any) -> None:
        await asyncio.sleep(float(metadata["seconds"]))
        await self._call(handler)

    async def _run_cron(self, metadata: dict[str, Any], handler: Any) -> None:
        expression = metadata["expression"]
        while True:
            delay = self.next_cron_delay(expression)
            await asyncio.sleep(delay)
            await self._call(handler)

    def next_cron_delay(self, expression: str, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        iterator = croniter(expression, now, second_at_beginning=len(expression.split()) == 6)
        next_run = iterator.get_next(datetime)
        return max((next_run - now).total_seconds(), 0.0)

    async def _call(self, handler: Any) -> None:
        result = handler()
        if inspect.isawaitable(result):
            await result
