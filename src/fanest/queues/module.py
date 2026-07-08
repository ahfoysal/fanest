import inspect
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fanest import Injectable, Module


@dataclass(frozen=True)
class Job:
    id: str
    queue: str
    name: str
    data: Any
    attempts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def Processor(queue: str):
    def decorator(cls):
        setattr(cls, "__fanest_queue__", queue)
        return Injectable()(cls)

    return decorator


def Process(name: str = "default"):
    def decorator(handler):
        setattr(handler, "__fanest_process__", name)
        return handler

    return decorator


@Injectable()
class QueueService:
    def __init__(self):
        self._handlers: dict[tuple[str, str], list[Any]] = {}
        self._jobs: list[Job] = []

    def register_processor(self, queue: str, name: str, handler: Any) -> None:
        self._handlers.setdefault((queue, name), []).append(handler)

    async def add(
        self,
        queue: str,
        data: Any,
        *,
        name: str = "default",
        job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Job:
        job = Job(
            id=job_id or str(uuid4()),
            queue=queue,
            name=name,
            data=data,
            metadata=metadata or {},
        )
        self._jobs.append(job)
        for handler in self._handlers.get((queue, name), []):
            result = handler(job)
            if inspect.isawaitable(result):
                await result
        return job

    def jobs(self, queue: str | None = None) -> list[Job]:
        if queue is None:
            return list(self._jobs)
        return [job for job in self._jobs if job.queue == queue]

    def clear(self) -> None:
        self._jobs.clear()


class QueueModule:
    @staticmethod
    def for_root() -> type:
        @Module(providers=[QueueService], exports=[QueueService])
        class DynamicQueueModule:
            pass

        return DynamicQueueModule
