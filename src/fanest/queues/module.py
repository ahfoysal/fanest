import inspect
import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from fanest import Inject, Injectable, Module, Optional, use_value
from fanest.core.providers import token, use_factory

QUEUE_OPTIONS = token("QUEUE_OPTIONS")


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


class QueueBackend(Protocol):
    async def add(self, job: Job) -> Job: ...

    def jobs(self, queue: str | None = None) -> list[Job]: ...

    def clear(self) -> None: ...


class MemoryQueueBackend:
    def __init__(self) -> None:
        self._jobs: list[Job] = []

    async def add(self, job: Job) -> Job:
        self._jobs.append(job)
        return job

    def jobs(self, queue: str | None = None) -> list[Job]:
        if queue is None:
            return list(self._jobs)
        return [job for job in self._jobs if job.queue == queue]

    def clear(self) -> None:
        self._jobs.clear()


class RedisStreamQueueBackend:
    def __init__(self, *, url: str = "redis://localhost:6379/0", prefix: str = "fanest:queue:") -> None:
        try:
            import redis  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover - exercised without redis installed
            raise ImportError(
                "RedisStreamQueueBackend requires the 'redis' package. "
                "Install it with: pip install 'fanest[redis]'"
            ) from exc
        self.prefix = prefix
        self._client = redis.Redis.from_url(url)

    async def add(self, job: Job) -> Job:
        self._client.xadd(
            self._stream(job.queue),
            {
                "id": job.id,
                "name": job.name,
                "data": json_dumps(job.data),
                "attempts": str(job.attempts),
                "max_attempts": str(job.max_attempts),
                "delay": str(job.delay),
                "metadata": json_dumps(job.metadata),
            },
        )
        return job

    def jobs(self, queue: str | None = None) -> list[Job]:
        queues = [queue] if queue is not None else self._queues()
        jobs: list[Job] = []
        for queue_name in queues:
            if queue_name is None:
                continue
            for _, fields in self._client.xrange(self._stream(queue_name)):
                decoded = {
                    self._decode(key): self._decode(value)
                    for key, value in fields.items()
                }
                jobs.append(
                    Job(
                        id=decoded["id"],
                        queue=queue_name,
                        name=decoded.get("name", "default"),
                        data=json_loads(decoded.get("data", "null")),
                        attempts=int(decoded.get("attempts", "0")),
                        max_attempts=int(decoded.get("max_attempts", "1")),
                        delay=float(decoded.get("delay", "0")),
                        metadata=json_loads(decoded.get("metadata", "{}")),
                    )
                )
        return jobs

    def clear(self) -> None:
        for queue in self._queues():
            self._client.delete(self._stream(queue))

    def _stream(self, queue: str) -> str:
        return f"{self.prefix}{queue}"

    def _queues(self) -> list[str]:
        prefix = self.prefix.encode()
        return [
            self._decode(key)[len(self.prefix):]
            for key in self._client.scan_iter(match=f"{self.prefix}*")
            if self._decode(key).encode().startswith(prefix)
        ]

    def _decode(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode()
        return str(value)


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
    def __init__(self, options: dict[str, Any] | None = Optional(QUEUE_OPTIONS)):
        options = options if isinstance(options, dict) else {}
        self._handlers: dict[tuple[str, str], list[Any]] = {}
        backend = options.get("backend")
        if backend is not None:
            self.backend: QueueBackend = backend
        elif options.get("redis_url"):
            self.backend = RedisStreamQueueBackend(
                url=options["redis_url"],
                prefix=options.get("redis_prefix", "fanest:queue:"),
            )
        else:
            self.backend = MemoryQueueBackend()

    def register_processor(self, queue: str, name: str, handler: Any) -> None:
        handlers = self._handlers.setdefault((queue, name), [])
        if self._handler_key(handler) not in {self._handler_key(item) for item in handlers}:
            handlers.append(handler)

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
        await self.backend.add(job)
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
        return self.backend.jobs(queue)

    def clear(self) -> None:
        self.backend.clear()

    def queue(self, name: str) -> QueueRef:
        return QueueRef(self, name)

    def _handler_key(self, handler: Any) -> Any:
        return getattr(
            handler,
            "__fanest_registration_key__",
            (
                getattr(getattr(handler, "__self__", None), "__class__", None),
                getattr(getattr(handler, "__func__", None), "__name__", None),
                handler,
            ),
        )


class QueueModule:
    @staticmethod
    def for_root(
        *,
        is_global: bool = True,
        backend: QueueBackend | None = None,
        redis_url: str | None = None,
        redis_prefix: str = "fanest:queue:",
    ) -> type:
        options = {"backend": backend, "redis_url": redis_url, "redis_prefix": redis_prefix}

        @Module(
            providers=[use_value(QUEUE_OPTIONS, options), QueueService],
            exports=[QueueService],
            global_module=is_global,
        )
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


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value)


def json_loads(value: str) -> Any:
    import json

    return json.loads(value)
