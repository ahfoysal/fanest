import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    kind: str
    task: asyncio.Task[Any]
    metadata: dict[str, Any]

    @property
    def done(self) -> bool:
        return self.task.done()

    @property
    def cancelled(self) -> bool:
        return self.task.cancelled()

    def cancel(self) -> None:
        self.task.cancel()


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
