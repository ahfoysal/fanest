import inspect
import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fanest import Inject, Injectable, Module
from fanest.core.providers import token, use_factory


@dataclass(frozen=True)
class Job:
    id: str
    queue: str
    name: str
    data: Any
    attempts: int = 0
    max_attempts: int = 1
    delay: float = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def queue_token(name: str):
    return token(f"QUEUE:{name}")


def InjectQueue(name: str):
    return Inject(queue_token(name))


class QueueRef:
    def __init__(self, queue: "QueueService", name: str):
        self.queue = queue
        self.name = name

    async def add(
        self,
        *args: Any,
        name: str = "default",
        job_id: str | None = None,
        attempts: int = 1,
        delay: float = 0,
        metadata: dict[str, Any] | None = None,
        **options: Any,
    ) -> Job:
        if len(args) == 2:
            name = args[0]
            data = args[1]
        elif len(args) == 1:
            data = args[0]
        else:
            raise TypeError("QueueRef.add expects data or name, data")
        job_id = options.pop("job_id", job_id)
        attempts = options.pop("attempts", attempts)
        delay = options.pop("delay", delay)
        metadata = {**(metadata or {}), **options}
        return await self.queue.add(
            self.name,
            data,
            name=name,
            job_id=job_id,
            attempts=attempts,
            delay=delay,
            metadata=metadata,
        )

    def jobs(self) -> list[Job]:
        return self.queue.jobs(self.name)


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
        attempts: int = 1,
        delay: float = 0,
        metadata: dict[str, Any] | None = None,
    ) -> Job:
        job = Job(
            id=job_id or str(uuid4()),
            queue=queue,
            name=name,
            data=data,
            max_attempts=attempts,
            delay=delay,
            metadata=metadata or {},
        )
        self._jobs.append(job)
        if delay > 0:
            await asyncio.sleep(delay)
        for handler in self._handlers.get((queue, name), []):
            await self._run_handler(handler, job)
        return job

    async def _run_handler(self, handler: Any, job: Job) -> None:
        last_error: Exception | None = None
        for attempt in range(1, job.max_attempts + 1):
            attempt_job = Job(
                id=job.id,
                queue=job.queue,
                name=job.name,
                data=job.data,
                attempts=attempt,
                max_attempts=job.max_attempts,
                delay=job.delay,
                metadata=job.metadata,
            )
            try:
                result = handler(attempt_job)
                if inspect.isawaitable(result):
                    await result
                return
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error

    def jobs(self, queue: str | None = None) -> list[Job]:
        if queue is None:
            return list(self._jobs)
        return [job for job in self._jobs if job.queue == queue]

    def clear(self) -> None:
        self._jobs.clear()

    def queue(self, name: str) -> QueueRef:
        return QueueRef(self, name)


class QueueModule:
    @staticmethod
    def for_root(*, is_global: bool = True) -> type:
        @Module(providers=[QueueService], exports=[QueueService], global_module=is_global)
        class DynamicQueueModule:
            pass

        return DynamicQueueModule

    @staticmethod
    def register_queue(name: str) -> type:
        @Module(
            providers=[
                use_factory(queue_token(name), lambda service: service.queue(name), inject=[QueueService])
            ],
            exports=[queue_token(name)],
        )
        class DynamicQueueFeatureModule:
            pass

        return DynamicQueueFeatureModule


BullModule = QueueModule
