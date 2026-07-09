import asyncio
import inspect
import logging
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from fanest.schedule.registry import SchedulerRegistry

logger = logging.getLogger("fanest.schedule")


class ScheduleRunner:
    def __init__(self, providers: Iterable[Any], registry: SchedulerRegistry | None = None) -> None:
        self.providers = providers
        self.tasks: list[asyncio.Task] = []
        self.running_jobs: set[asyncio.Task] = set()
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
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for task in list(self.running_jobs):
            task.cancel()
        if self.running_jobs:
            await asyncio.gather(*self.running_jobs, return_exceptions=True)
        self.tasks.clear()
        self.running_jobs.clear()

    def _schedule(self, name: str, metadata: dict[str, Any], coroutine: Any) -> None:
        if metadata.get("disabled"):
            coroutine.close()
            return
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.append(task)
        self.registry.add(name, metadata["type"], task, metadata)

    def _default_name(self, provider: Any, handler_name: str, kind: str) -> str:
        return f"{provider.__class__.__name__}.{handler_name}:{kind}"

    async def _run_interval(self, metadata: dict[str, Any], handler: Any) -> None:
        delay = float(metadata["seconds"])
        next_run = time.monotonic() + delay
        while True:
            await asyncio.sleep(max(next_run - time.monotonic(), 0.0))
            task = asyncio.create_task(self._safe_call(handler))
            self.running_jobs.add(task)
            task.add_done_callback(self.running_jobs.discard)
            next_run += delay

    async def _run_timeout(self, metadata: dict[str, Any], handler: Any) -> None:
        await asyncio.sleep(float(metadata["seconds"]))
        await self._safe_call(handler)

    async def _run_cron(self, metadata: dict[str, Any], handler: Any) -> None:
        expression = metadata["expression"]
        wait_for_completion = bool(metadata.get("wait_for_completion"))
        running_task: asyncio.Task[Any] | None = None
        next_run = self.next_cron_datetime(
            expression,
            time_zone=metadata.get("time_zone"),
            utc_offset=metadata.get("utc_offset"),
        )
        while True:
            delay = self._delay_until(next_run)
            await asyncio.sleep(delay)
            if not wait_for_completion or running_task is None or running_task.done():
                running_task = asyncio.create_task(self._safe_call(handler))
                self.running_jobs.add(running_task)
                running_task.add_done_callback(self.running_jobs.discard)
            next_run = self.next_cron_datetime(
                expression,
                next_run,
                time_zone=metadata.get("time_zone"),
                utc_offset=metadata.get("utc_offset"),
            )

    def next_cron_delay(
        self,
        expression: str,
        now: datetime | None = None,
        *,
        time_zone: str | None = None,
        utc_offset: int | None = None,
    ) -> float:
        base = self._cron_now(now, time_zone=time_zone, utc_offset=utc_offset)
        next_run = self.next_cron_datetime(expression, base, time_zone=time_zone, utc_offset=utc_offset)
        return max((next_run - base).total_seconds(), 0.0)

    def next_cron_datetime(
        self,
        expression: str,
        now: datetime | None = None,
        *,
        time_zone: str | None = None,
        utc_offset: int | None = None,
    ) -> datetime:
        base = self._cron_now(now, time_zone=time_zone, utc_offset=utc_offset)
        second_at_beginning = len(expression.split()) == 6
        iterator = croniter(expression, base, second_at_beginning=second_at_beginning)
        next_run = iterator.get_next(datetime)
        if next_run.tzinfo is None:
            return next_run.replace(tzinfo=base.tzinfo)
        return next_run

    def _cron_now(
        self,
        now: datetime | None = None,
        *,
        time_zone: str | None = None,
        utc_offset: int | None = None,
    ) -> datetime:
        cron_tz = self._cron_timezone(time_zone=time_zone, utc_offset=utc_offset)
        if now is None:
            return datetime.now(cron_tz)
        if now.tzinfo is None:
            return now.replace(tzinfo=cron_tz)
        return now.astimezone(cron_tz)

    def _cron_timezone(self, *, time_zone: str | None = None, utc_offset: int | None = None) -> tzinfo:
        if time_zone and utc_offset is not None:
            raise ValueError("CronJob cannot define both time_zone and utc_offset.")
        if time_zone:
            return ZoneInfo(time_zone)
        if utc_offset is not None:
            return timezone(timedelta(minutes=utc_offset))
        return timezone.utc

    def _delay_until(self, run_at: datetime) -> float:
        now = datetime.now(run_at.tzinfo or timezone.utc)
        return max((run_at - now).total_seconds(), 0.0)

    async def _call(self, handler: Any) -> None:
        result = handler()
        if inspect.isawaitable(result):
            await result

    async def _safe_call(self, handler: Any) -> None:
        try:
            await self._call(handler)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled job %r failed", getattr(handler, "__qualname__", handler))
