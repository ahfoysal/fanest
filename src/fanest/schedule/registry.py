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

    def get(self, name: str) -> ScheduledJob:
        try:
            return self._jobs[name]
        except KeyError as exc:
            raise KeyError(f"No scheduled job registered for {name!r}.") from exc

    def list(self, kind: str | None = None) -> list[ScheduledJob]:
        jobs = list(self._jobs.values())
        if kind is None:
            return jobs
        return [job for job in jobs if job.kind == kind]

    def delete(self, name: str, *, cancel: bool = True) -> None:
        job = self.get(name)
        if cancel:
            job.cancel()
        del self._jobs[name]

    def clear(self, *, cancel: bool = True) -> None:
        for name in list(self._jobs):
            self.delete(name, cancel=cancel)
