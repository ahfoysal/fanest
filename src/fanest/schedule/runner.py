import asyncio
import inspect
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any

from croniter import croniter


class ScheduleRunner:
    def __init__(self, providers: Iterable[Any]) -> None:
        self.providers = providers
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for provider in self.providers:
            for _, handler in inspect.getmembers(provider, predicate=callable):
                metadata = getattr(handler, "__fanest_schedule__", None)
                if metadata is None:
                    continue
                if metadata["type"] == "interval":
                    self.tasks.append(asyncio.create_task(self._run_interval(metadata, handler)))
                elif metadata["type"] == "cron":
                    self.tasks.append(asyncio.create_task(self._run_cron(metadata, handler)))
                else:
                    raise ValueError(f"Unknown schedule type: {metadata['type']}")

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _run_interval(self, metadata: dict[str, Any], handler: Any) -> None:
        delay = float(metadata["seconds"])
        while True:
            await asyncio.sleep(delay)
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
