import asyncio
import inspect
from collections.abc import Iterable
from typing import Any


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
                delay = self._delay(metadata)
                self.tasks.append(asyncio.create_task(self._run_every(delay, handler)))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _run_every(self, delay: float, handler: Any) -> None:
        while True:
            await asyncio.sleep(delay)
            result = handler()
            if inspect.isawaitable(result):
                await result

    def _delay(self, metadata: dict[str, Any]) -> float:
        if metadata["type"] == "interval":
            return float(metadata["seconds"])
        if metadata["type"] == "cron":
            return self._cron_delay(metadata["expression"])
        raise ValueError(f"Unknown schedule type: {metadata['type']}")

    def _cron_delay(self, expression: str) -> float:
        fields = expression.split()
        if len(fields) == 6:
            return self._field_delay(fields[0], unit=1)
        if len(fields) == 5:
            return self._field_delay(fields[0], unit=60)
        raise ValueError("Cron expressions must have 5 fields or 6 fields with seconds.")

    def _field_delay(self, field: str, *, unit: int) -> float:
        if field == "*":
            return float(unit)
        if field.startswith("*/"):
            return float(field[2:]) * unit
        try:
            return float(field) * unit
        except ValueError as exc:
            raise ValueError(f"Unsupported cron field: {field}") from exc
