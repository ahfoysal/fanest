from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    kind: str
    task: asyncio.Task[Any]
    metadata: dict[str, Any]
    run_count: int = 0
    error_count: int = 0
    last_run_at: float | None = None
    last_error: str | None = None

    @property
    def done(self) -> bool:
        return self.task.done()

    @property
    def cancelled(self) -> bool:
        return self.task.cancelled()

    def cancel(self) -> None:
        self.task.cancel()

    def record_success(self, *, at: float) -> None:
        object.__setattr__(self, "run_count", self.run_count + 1)
        object.__setattr__(self, "last_run_at", at)

    def record_error(self, error: BaseException, *, at: float) -> None:
        object.__setattr__(self, "run_count", self.run_count + 1)
        object.__setattr__(self, "error_count", self.error_count + 1)
        object.__setattr__(self, "last_run_at", at)
        object.__setattr__(self, "last_error", str(error))


class SchedulerRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, ScheduledJob] = {}

    def add(self, name: str, kind: str, task: asyncio.Task[Any], metadata: dict[str, Any]) -> None:
        if name in self._jobs:
            raise ValueError(f"Scheduled job {name!r} is already registered.")
        self._jobs[name] = ScheduledJob(name=name, kind=kind, task=task, metadata=dict(metadata))

    def add_cron_job(self, name: str, task: asyncio.Task[Any], metadata: dict[str, Any] | None = None) -> None:
        self.add(name, "cron", task, metadata or {})

    def add_interval(self, name: str, task: asyncio.Task[Any], metadata: dict[str, Any] | None = None) -> None:
        self.add(name, "interval", task, metadata or {})

    def add_timeout(self, name: str, task: asyncio.Task[Any], metadata: dict[str, Any] | None = None) -> None:
        self.add(name, "timeout", task, metadata or {})

    def get(self, name: str) -> ScheduledJob:
        try:
            return self._jobs[name]
        except KeyError as exc:
            raise KeyError(f"No scheduled job registered for {name!r}.") from exc

    def has(self, name: str) -> bool:
        return name in self._jobs

    def names(self, kind: str | None = None) -> list[str]:
        return [job.name for job in self.list(kind)]

    def get_cron_job(self, name: str) -> ScheduledJob:
        return self._get_kind(name, "cron")

    def get_interval(self, name: str) -> ScheduledJob:
        return self._get_kind(name, "interval")

    def get_timeout(self, name: str) -> ScheduledJob:
        return self._get_kind(name, "timeout")

    def list(self, kind: str | None = None) -> list[ScheduledJob]:
        jobs = list(self._jobs.values())
        if kind is None:
            return jobs
        return [job for job in jobs if job.kind == kind]

    def get_cron_jobs(self) -> dict[str, ScheduledJob]:
        return {job.name: job for job in self.list("cron")}

    def get_intervals(self) -> list[str]:
        return [job.name for job in self.list("interval")]

    def get_timeouts(self) -> list[str]:
        return [job.name for job in self.list("timeout")]

    def delete(self, name: str, *, cancel: bool = True) -> None:
        job = self.get(name)
        if cancel:
            job.cancel()
        del self._jobs[name]

    def cancel(self, name: str) -> None:
        self.get(name).cancel()

    def delete_cron_job(self, name: str) -> None:
        self.delete(name)

    def delete_interval(self, name: str) -> None:
        self.delete(name)

    def delete_timeout(self, name: str) -> None:
        self.delete(name)

    def clear(self, *, cancel: bool = True) -> None:
        for name in list(self._jobs):
            self.delete(name, cancel=cancel)

    def _get_kind(self, name: str, kind: str) -> ScheduledJob:
        job = self.get(name)
        if job.kind != kind:
            raise KeyError(f"Scheduled job {name!r} is not a {kind} job.")
        return job
