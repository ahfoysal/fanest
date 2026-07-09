from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any

from fanest import Injectable, Module, Optional, use_value
from fanest.core.providers import token

WORKER_OPTIONS = token("WORKER_OPTIONS")


class WorkerTaskNotFoundError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"No worker task handler registered for: {name}")
        self.name = name


class WorkerTaskConflictError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Worker task handler already registered for: {name}")
        self.name = name


@dataclass(frozen=True)
class WorkerTask:
    name: str
    handler: Any
    registered_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class WorkerTaskRun:
    name: str
    payload: Any
    started_at: float
    finished_at: float | None = None
    result: Any = None
    error: str | None = None


@dataclass(frozen=True)
class WorkerStats:
    registered: int
    active: int
    completed: int
    failed: int


def TaskHandler(name: str):
    def decorator(handler):
        setattr(handler, "__fanest_task_handler__", name)
        return handler

    return decorator


@Injectable()
class WorkerService:
    def __init__(self, options: dict[str, Any] | None = Optional(WORKER_OPTIONS)) -> None:
        options = options if isinstance(options, dict) else {}
        self._handlers: dict[str, WorkerTask] = {}
        self._history: list[WorkerTaskRun] = []
        self._active: set[asyncio.Task[Any]] = set()
        self._semaphore = asyncio.Semaphore(max(int(options.get("concurrency", 0)), 1)) if options else None

    def register(self, name: str, handler: Any) -> None:
        existing = self._handlers.get(name)
        if existing is not None and self._handler_key(existing.handler) == self._handler_key(handler):
            return
        if existing is not None:
            raise WorkerTaskConflictError(name)
        self._handlers[name] = WorkerTask(name=name, handler=handler)

    async def run(self, name: str, payload: Any = None) -> Any:
        task = self._handlers.get(name)
        if task is None:
            raise WorkerTaskNotFoundError(name)
        if self._semaphore is None:
            return await self._run_task(task, payload)
        async with self._semaphore:
            return await self._run_task(task, payload)

    def run_background(self, name: str, payload: Any = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(self.run(name, payload), name=f"fanest-worker:{name}")
        self._active.add(task)
        task.add_done_callback(self._active.discard)
        return task

    async def _run_task(self, task: WorkerTask, payload: Any) -> Any:
        started_at = time.monotonic()
        try:
            result = task.handler(payload)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self._history.append(
                WorkerTaskRun(
                    name=task.name,
                    payload=payload,
                    started_at=started_at,
                    finished_at=time.monotonic(),
                    error=str(exc),
                )
            )
            raise
        self._history.append(
            WorkerTaskRun(
                name=task.name,
                payload=payload,
                started_at=started_at,
                finished_at=time.monotonic(),
                result=result,
            )
        )
        return result

    async def run_many(
        self,
        jobs: list[tuple[str, Any]],
        *,
        concurrent: bool = False,
        return_exceptions: bool = False,
    ) -> list[Any]:
        if not concurrent:
            results: list[Any] = []
            for name, payload in jobs:
                try:
                    results.append(await self.run(name, payload))
                except Exception as exc:
                    if not return_exceptions:
                        raise
                    results.append(exc)
            return results
        gathered = await asyncio.gather(
            *(self.run(name, payload) for name, payload in jobs),
            return_exceptions=return_exceptions,
        )
        return list(gathered)

    def has(self, name: str) -> bool:
        return name in self._handlers

    def list(self) -> list[str]:
        return sorted(self._handlers)

    def tasks(self) -> list[WorkerTask]:
        return [self._handlers[name] for name in self.list()]

    def active_count(self) -> int:
        return len(self._active)

    def stats(self) -> WorkerStats:
        failed = len([run for run in self._history if run.error is not None])
        return WorkerStats(
            registered=len(self._handlers),
            active=self.active_count(),
            completed=len(self._history) - failed,
            failed=failed,
        )

    def history(self, name: str | None = None) -> list[WorkerTaskRun]:
        if name is None:
            return list(self._history)
        return [run for run in self._history if run.name == name]

    def clear_history(self) -> None:
        self._history.clear()

    async def shutdown(self) -> None:
        for task in list(self._active):
            task.cancel()
        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)
        self._active.clear()

    def _handler_key(self, handler: Any) -> Any:
        return getattr(handler, "__fanest_registration_key__", handler)


class WorkerModule:
    @staticmethod
    def for_root(*, is_global: bool = False, concurrency: int | None = None) -> type:
        options = {"concurrency": concurrency} if concurrency is not None else {}

        @Module(
            providers=[use_value(WORKER_OPTIONS, options), WorkerService],
            exports=[WorkerService],
            global_module=is_global,
        )
        class DynamicWorkerModule:
            pass

        return DynamicWorkerModule
