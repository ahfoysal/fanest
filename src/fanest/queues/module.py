import inspect
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from uuid import uuid4

from fanest import Inject, Injectable, Module, Optional, use_value
from fanest.core.providers import token, use_factory

QUEUE_OPTIONS = token("QUEUE_OPTIONS")
logger = logging.getLogger("fanest.queues")


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
    status: str = "waiting"
    failed_reason: str | None = None
    enqueued_at: float = field(default_factory=time.monotonic)
    processed_at: float | None = None
    finished_at: float | None = None


@dataclass(frozen=True)
class QueueStats:
    queue: str | None
    waiting: int
    active: int
    completed: int
    failed: int
    dead_letter: int
    delayed: int


@dataclass(frozen=True)
class JobAttempt:
    job_id: str
    attempt: int
    started_at: float
    finished_at: float
    success: bool
    error: str | None = None


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
        backoff: float | dict[str, Any] | None = None,
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
        backoff = options.pop("backoff", backoff)
        metadata = {**(metadata or {}), **options}
        return await self.queue.add(
            self.name,
            data,
            name=name,
            job_id=job_id,
            attempts=attempts,
            delay=delay,
            backoff=backoff,
            metadata=metadata,
        )

    def jobs(self) -> list[Job]:
        return self.queue.jobs(self.name)

    def completed(self) -> list[Job]:
        return self.queue.completed_jobs(self.name)

    def failed(self) -> list[Job]:
        return self.queue.failed_jobs(self.name)

    def dead_letter(self) -> list[Job]:
        return self.queue.dead_letter_jobs(self.name)

    def stats(self) -> QueueStats:
        return self.queue.stats(self.name)


class QueueBackend(Protocol):
    async def add(self, job: Job) -> Job: ...

    def update(self, job: Job) -> Job: ...

    def jobs(self, queue: str | None = None) -> list[Job]: ...

    def clear(self) -> None: ...


class MemoryQueueBackend:
    def __init__(self) -> None:
        self._jobs: list[Job] = []

    async def add(self, job: Job) -> Job:
        self._jobs.append(job)
        return job

    def update(self, job: Job) -> Job:
        for index, existing in enumerate(self._jobs):
            if existing.id == job.id:
                self._jobs[index] = job
                return job
        self._jobs.append(job)
        return job

    def jobs(self, queue: str | None = None) -> list[Job]:
        if queue is None:
            return list(self._jobs)
        return [job for job in self._jobs if job.queue == queue]

    def clear(self) -> None:
        self._jobs.clear()


class RedisStreamQueueBackend:
    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379/0",
        prefix: str = "fanest:queue:",
        client: Any | None = None,
    ) -> None:
        self.prefix = prefix
        if client is not None:
            self._client = client
            return
        try:
            import redis  # type: ignore[reportMissingImports]
        except ImportError as exc:  # pragma: no cover - exercised without redis installed
            raise ImportError(
                "RedisStreamQueueBackend requires the 'redis' package. "
                "Install it with: pip install 'fanest[redis]'"
            ) from exc
        self._client = redis.Redis.from_url(url)

    async def add(self, job: Job) -> Job:
        self._write_job(job)
        return job

    def update(self, job: Job) -> Job:
        self._write_job(job)
        return job

    def jobs(self, queue: str | None = None) -> list[Job]:
        queues = [queue] if queue is not None else self._queues()
        jobs_by_id: dict[str, Job] = {}
        for queue_name in queues:
            if queue_name is None:
                continue
            entries = self._client.xrange(self._stream(queue_name)) or []
            for _, fields in entries:
                if fields is None:
                    continue
                decoded_fields = cast(dict[Any, Any], fields)
                job = self._decode_job(queue_name, decoded_fields)
                jobs_by_id[job.id] = job
        return list(jobs_by_id.values())

    def clear(self) -> None:
        for queue in self._queues():
            self._client.delete(self._stream(queue))

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    def _stream(self, queue: str) -> str:
        return f"{self.prefix}{queue}"

    def _write_job(self, job: Job) -> None:
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
                "status": job.status,
                "failed_reason": job.failed_reason or "",
                "enqueued_at": str(job.enqueued_at),
                "processed_at": "" if job.processed_at is None else str(job.processed_at),
                "finished_at": "" if job.finished_at is None else str(job.finished_at),
            },
        )

    def _decode_job(self, queue_name: str, fields: dict[Any, Any]) -> Job:
        decoded = {self._decode(key): self._decode(value) for key, value in fields.items()}
        return Job(
            id=decoded["id"],
            queue=queue_name,
            name=decoded.get("name", "default"),
            data=json_loads(decoded.get("data", "null")),
            attempts=int(decoded.get("attempts", "0")),
            max_attempts=int(decoded.get("max_attempts", "1")),
            delay=float(decoded.get("delay", "0")),
            metadata=json_loads(decoded.get("metadata", "{}")),
            status=decoded.get("status", "waiting"),
            failed_reason=decoded.get("failed_reason") or None,
            enqueued_at=float(decoded.get("enqueued_at", "0") or "0"),
            processed_at=_optional_float(decoded.get("processed_at")),
            finished_at=_optional_float(decoded.get("finished_at")),
        )

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
        self._active: dict[str, Job] = {}
        self._completed: list[Job] = []
        self._failed: list[Job] = []
        self._dead_letter: list[Job] = []
        self._delayed: dict[str, Job] = {}
        self._attempts: dict[str, list[JobAttempt]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        backend = options.get("backend")
        if backend is not None:
            self.backend: QueueBackend = backend
        elif options.get("redis_url") or options.get("redis_client") is not None:
            self.backend = RedisStreamQueueBackend(
                url=options.get("redis_url", "redis://localhost:6379/0"),
                prefix=options.get("redis_prefix", "fanest:queue:"),
                client=options.get("redis_client"),
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
        backoff: float | dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Job:
        if attempts < 1:
            raise ValueError("Queue job attempts must be at least 1.")
        if delay < 0:
            raise ValueError("Queue job delay cannot be negative.")
        metadata = metadata or {}
        if backoff is not None:
            metadata = {**metadata, "backoff": backoff}
        job = Job(
            id=job_id or str(uuid4()),
            queue=queue,
            name=name,
            data=data,
            max_attempts=attempts,
            delay=delay,
            metadata=metadata,
        )
        await self.backend.add(job)
        if delay > 0:
            delayed_job = self._copy_job(job, status="delayed")
            self._delayed[job.id] = delayed_job
            await self._update_backend(delayed_job)
            self._schedule_background(self._dispatch_after_delay(job))
            return job
        await self._dispatch_job(job, raise_on_failure=True)
        return job

    async def _dispatch_after_delay(self, job: Job) -> None:
        try:
            await asyncio.sleep(job.delay)
            self._delayed.pop(job.id, None)
            await self._dispatch_job(job, raise_on_failure=False)
        except asyncio.CancelledError:
            self._delayed.pop(job.id, None)
            raise

    async def _dispatch_job(self, job: Job, *, raise_on_failure: bool) -> None:
        handlers = self._handlers.get((job.queue, job.name), [])
        if not handlers:
            return
        active_job = self._copy_job(job, status="active", processed_at=time.monotonic())
        self._active[job.id] = active_job
        await self._update_backend(active_job)
        try:
            for handler in handlers:
                await self._run_handler(handler, job)
        except Exception as exc:
            failed = self._copy_job(
                job,
                attempts=job.max_attempts,
                status="failed",
                failed_reason=str(exc),
                finished_at=time.monotonic(),
            )
            self._failed.append(failed)
            self._dead_letter.append(failed)
            await self._update_backend(failed)
            if raise_on_failure:
                raise
            logger.exception("Queue job %s/%s failed permanently", job.queue, job.name)
        else:
            completed = self._copy_job(job, status="completed", finished_at=time.monotonic())
            self._completed.append(completed)
            await self._update_backend(completed)
        finally:
            self._active.pop(job.id, None)

    async def _run_handler(self, handler: Any, job: Job) -> None:
        last_error: Exception | None = None
        for attempt in range(1, job.max_attempts + 1):
            attempt_job = self._copy_job(
                job,
                attempts=attempt,
                status="active",
                processed_at=time.monotonic(),
            )
            try:
                result = handler(attempt_job)
                if inspect.isawaitable(result):
                    await result
                self._record_attempt(attempt_job, success=True)
                return
            except Exception as exc:
                last_error = exc
                self._record_attempt(attempt_job, success=False, error=str(exc))
                if attempt < job.max_attempts:
                    await asyncio.sleep(self._backoff_delay(job, attempt))
        if last_error is not None:
            raise last_error

    def jobs(self, queue: str | None = None) -> list[Job]:
        return self.backend.jobs(queue)

    def waiting_jobs(self, queue: str | None = None) -> list[Job]:
        hidden_ids = {
            *self._active,
            *self._delayed,
            *(job.id for job in self._completed),
            *(job.id for job in self._failed),
        }
        return [job for job in self.jobs(queue) if job.id not in hidden_ids]

    def get_job(self, job_id: str) -> Job:
        for collection in (
            self._active.values(),
            self._delayed.values(),
            self._completed,
            self._failed,
            self._dead_letter,
            self.jobs(),
        ):
            for job in collection:
                if job.id == job_id:
                    return job
        raise KeyError(f"No queue job registered for {job_id!r}.")

    def attempts(self, job_id: str) -> list[JobAttempt]:
        return list(self._attempts.get(job_id, []))

    def active_jobs(self, queue: str | None = None) -> list[Job]:
        return self._filter_jobs(self._active.values(), queue)

    def delayed_jobs(self, queue: str | None = None) -> list[Job]:
        return self._filter_jobs(self._delayed.values(), queue)

    def completed_jobs(self, queue: str | None = None) -> list[Job]:
        return self._filter_jobs(self._completed, queue)

    def failed_jobs(self, queue: str | None = None) -> list[Job]:
        return self._filter_jobs(self._failed, queue)

    def dead_letter_jobs(self, queue: str | None = None) -> list[Job]:
        return self._filter_jobs(self._dead_letter, queue)

    def stats(self, queue: str | None = None) -> QueueStats:
        return QueueStats(
            queue=queue,
            waiting=len(self.waiting_jobs(queue)),
            active=len(self.active_jobs(queue)),
            completed=len(self.completed_jobs(queue)),
            failed=len(self.failed_jobs(queue)),
            dead_letter=len(self.dead_letter_jobs(queue)),
            delayed=len(self.delayed_jobs(queue)),
        )

    async def retry_failed(self, job_id: str) -> Job:
        for index, job in enumerate(self._failed):
            if job.id == job_id:
                del self._failed[index]
                retry = self._copy_job(job, attempts=0, status="waiting", failed_reason=None, finished_at=None)
                await self._dispatch_job(retry, raise_on_failure=True)
                return retry
        raise KeyError(f"No failed queue job registered for {job_id!r}.")

    def clean(self, *, status: str | None = None, queue: str | None = None) -> int:
        removed = 0
        if status in (None, "completed"):
            before = len(self._completed)
            self._completed = [job for job in self._completed if queue is not None and job.queue != queue]
            removed += before - len(self._completed)
        if status in (None, "failed"):
            before = len(self._failed)
            self._failed = [job for job in self._failed if queue is not None and job.queue != queue]
            removed += before - len(self._failed)
        if status in (None, "dead_letter"):
            before = len(self._dead_letter)
            self._dead_letter = [job for job in self._dead_letter if queue is not None and job.queue != queue]
            removed += before - len(self._dead_letter)
        if status not in (None, "completed", "failed", "dead_letter"):
            raise ValueError("Queue clean status must be completed, failed, dead_letter, or None.")
        return removed

    async def close(self) -> None:
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._delayed.clear()
        close = getattr(self.backend, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    async def wait_until_idle(self) -> None:
        while self._background_tasks or self._active:
            tasks = list(self._background_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await asyncio.sleep(0)

    def clear(self) -> None:
        self.backend.clear()
        self._active.clear()
        self._completed.clear()
        self._failed.clear()
        self._dead_letter.clear()
        self._delayed.clear()
        self._attempts.clear()
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

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

    def _schedule_background(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Background queue dispatch failed")

        task.add_done_callback(_done)

    async def _update_backend(self, job: Job) -> None:
        update = getattr(self.backend, "update", None)
        if update is None:
            return
        result = update(job)
        if inspect.isawaitable(result):
            await result

    def _backoff_delay(self, job: Job, attempt: int) -> float:
        backoff = job.metadata.get("backoff")
        if backoff is None:
            return 0.0
        if isinstance(backoff, (int, float)):
            return max(float(backoff), 0.0)
        if isinstance(backoff, dict):
            delay = max(float(backoff.get("delay", 0)), 0.0)
            if backoff.get("type") == "exponential":
                return delay * (2 ** max(attempt - 1, 0))
            return delay
        raise ValueError("Queue job backoff must be a number or a dictionary.")

    def _record_attempt(self, job: Job, *, success: bool, error: str | None = None) -> None:
        self._attempts.setdefault(job.id, []).append(
            JobAttempt(
                job_id=job.id,
                attempt=job.attempts,
                started_at=job.processed_at or time.monotonic(),
                finished_at=time.monotonic(),
                success=success,
                error=error,
            )
        )

    def _copy_job(self, job: Job, **changes: Any) -> Job:
        values = {
            "id": job.id,
            "queue": job.queue,
            "name": job.name,
            "data": job.data,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "delay": job.delay,
            "metadata": job.metadata,
            "status": job.status,
            "failed_reason": job.failed_reason,
            "enqueued_at": job.enqueued_at,
            "processed_at": job.processed_at,
            "finished_at": job.finished_at,
        }
        values.update(changes)
        return Job(**values)

    def _filter_jobs(self, jobs: Any, queue: str | None) -> list[Job]:
        result = list(jobs)
        if queue is None:
            return result
        return [job for job in result if job.queue == queue]


class QueueModule:
    @staticmethod
    def for_root(
        *,
        is_global: bool = True,
        backend: QueueBackend | None = None,
        redis_url: str | None = None,
        redis_prefix: str = "fanest:queue:",
        redis_client: Any | None = None,
    ) -> type:
        options = {
            "backend": backend,
            "redis_url": redis_url,
            "redis_prefix": redis_prefix,
            "redis_client": redis_client,
        }

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


def _optional_float(value: str | None) -> float | None:
    if value in {None, ""}:
        return None
    assert value is not None
    return float(value)
