import asyncio
import time
from datetime import datetime, timezone

import pytest

from fanest.schedule import Cron, CronExpression, CronJob, Interval
from fanest.schedule.runner import ScheduleRunner


def test_cron_delay_honors_time_zone():
    runner = ScheduleRunner([])
    now = datetime(2026, 7, 8, 17, 30, tzinfo=timezone.utc)

    assert runner.next_cron_delay("0 0 * * *", now, time_zone="Asia/Dhaka") == 1800


def test_cron_delay_honors_utc_offset():
    runner = ScheduleRunner([])
    now = datetime(2026, 7, 8, 23, 30, tzinfo=timezone.utc)

    assert runner.next_cron_delay("0 0 * * *", now, utc_offset=60) == 84600


def test_cron_rejects_conflicting_time_zone_options():
    runner = ScheduleRunner([])

    with pytest.raises(ValueError, match="both time_zone and utc_offset"):
        runner.next_cron_delay("0 0 * * *", time_zone="UTC", utc_offset=0)


class FlakyCronService:
    runs = 0

    @CronJob(CronExpression.EVERY_SECOND)
    async def flaky(self):
        type(self).runs += 1
        if type(self).runs == 1:
            raise RuntimeError("temporary cron failure")


@pytest.mark.anyio
async def test_cron_job_exception_does_not_kill_repeating_task():
    FlakyCronService.runs = 0
    runner = ScheduleRunner([FlakyCronService()])
    runner.start()

    await asyncio.sleep(2.2)
    await runner.stop()

    assert FlakyCronService.runs > 1


class SlowCronService:
    starts: list[float] = []

    @CronJob(CronExpression.EVERY_SECOND, wait_for_completion=True)
    async def slow(self):
        type(self).starts.append(time.monotonic())
        await asyncio.sleep(2.5)


@pytest.mark.anyio
async def test_cron_job_wait_for_completion_skips_overlapping_ticks():
    SlowCronService.starts = []
    runner = ScheduleRunner([SlowCronService()])
    runner.start()

    await asyncio.sleep(2.35)
    await runner.stop()

    assert len(SlowCronService.starts) == 1


class LongRunningIntervalService:
    started = 0

    @Interval(0.01)
    async def interval(self):
        type(self).started += 1
        await asyncio.sleep(60)


@pytest.mark.anyio
async def test_schedule_runner_stop_cancels_running_jobs_for_graceful_shutdown():
    LongRunningIntervalService.started = 0
    runner = ScheduleRunner([LongRunningIntervalService()])
    runner.start()

    await asyncio.sleep(0.04)
    await asyncio.wait_for(runner.stop(), timeout=0.25)

    assert LongRunningIntervalService.started > 0
    assert runner.running_jobs == set()


class NonOverlappingIntervalService:
    starts: list[float] = []

    @Interval(0.02, name="non-overlap", wait_for_completion=True)
    async def interval(self):
        type(self).starts.append(time.monotonic())
        await asyncio.sleep(0.06)


@pytest.mark.anyio
async def test_interval_wait_for_completion_skips_overlapping_ticks():
    NonOverlappingIntervalService.starts = []
    runner = ScheduleRunner([NonOverlappingIntervalService()])
    runner.start()

    await asyncio.sleep(0.075)
    await runner.stop()

    assert len(NonOverlappingIntervalService.starts) == 1


class ObservableScheduleService:
    runs = 0

    @Interval(0.01, name="observable")
    async def interval(self):
        type(self).runs += 1
        if type(self).runs == 1:
            raise RuntimeError("first tick failed")


@pytest.mark.anyio
async def test_scheduler_registry_records_run_and_error_counts():
    ObservableScheduleService.runs = 0
    runner = ScheduleRunner([ObservableScheduleService()])
    runner.start()

    await asyncio.sleep(0.035)
    job = runner.registry.get_interval("observable")
    await runner.stop()

    assert job.run_count >= 2
    assert job.error_count == 1
    assert job.last_error == "first tick failed"


# --- Regression: property getters must not fire at bootstrap ------------------------------------
class PropertySideEffectService:
    property_calls = 0
    interval_runs = 0

    @property
    def dangerous(self):
        # A property with a side effect (here, raising) that must never be touched by start().
        type(self).property_calls += 1
        raise RuntimeError("property getter fired at bootstrap")

    @Interval(0.01, name="prop-guard")
    async def tick(self):
        type(self).interval_runs += 1


@pytest.mark.anyio
async def test_start_does_not_invoke_property_getters_at_bootstrap():
    PropertySideEffectService.property_calls = 0
    PropertySideEffectService.interval_runs = 0
    runner = ScheduleRunner([PropertySideEffectService()])

    runner.start()
    assert PropertySideEffectService.property_calls == 0

    await asyncio.sleep(0.03)
    await runner.stop()

    # The property must never have been read, yet the decorated handler must still run.
    assert PropertySideEffectService.property_calls == 0
    assert PropertySideEffectService.interval_runs > 0


# --- Regression: invalid cron / bad timezone must fail fast at registration ---------------------
class InvalidCronService:
    @Cron("this is not a cron expression")
    async def bad(self):  # pragma: no cover - never scheduled
        pass


def test_invalid_cron_expression_fails_fast_at_registration():
    runner = ScheduleRunner([InvalidCronService()])

    with pytest.raises(ValueError, match="Invalid cron expression"):
        runner.start()


class ConflictingTimezoneService:
    @CronJob(CronExpression.EVERY_SECOND, time_zone="UTC", utc_offset=0)
    async def bad(self):  # pragma: no cover - never scheduled
        pass


def test_conflicting_timezone_options_fail_fast_at_registration():
    runner = ScheduleRunner([ConflictingTimezoneService()])

    with pytest.raises(ValueError, match="both time_zone and utc_offset"):
        runner.start()


# --- Regression: no missed-tick burst after an event-loop stall ---------------------------------
class BurstIntervalService:
    ticks: list[float] = []

    @Interval(0.02, name="burst")
    async def tick(self):
        type(self).ticks.append(time.monotonic())


@pytest.mark.anyio
async def test_interval_does_not_replay_missed_ticks_as_burst_after_stall():
    BurstIntervalService.ticks = []
    runner = ScheduleRunner([BurstIntervalService()])
    runner.start()

    await asyncio.sleep(0.05)  # a few normal ticks
    time.sleep(0.3)  # synchronously stall the event loop (~15 missed 20ms ticks)
    await asyncio.sleep(0.05)  # let the scheduler recover
    await runner.stop()

    ticks = BurstIntervalService.ticks
    delay = 0.02
    # A burst shows up as many near-instant executions replayed back-to-back. With catch-up
    # semantics the stall produces at most a single recovery tick, not a cluster.
    tiny_gaps = sum(1 for a, b in zip(ticks, ticks[1:]) if (b - a) < delay * 0.5)
    assert tiny_gaps <= 1, ticks


# --- Regression: same-named provider classes must not collide onto one job name -----------------
def _make_tasks_service():
    class TasksService:
        runs = 0

        @Interval(0.01)
        async def handle(self):
            type(self).runs += 1

    return TasksService


@pytest.mark.anyio
async def test_same_named_provider_classes_do_not_drop_jobs():
    ServiceA = _make_tasks_service()
    ServiceB = _make_tasks_service()
    # Genuinely identical implicit name base (same module, qualname, and method name).
    assert ServiceA.__qualname__ == ServiceB.__qualname__
    ServiceA.runs = 0
    ServiceB.runs = 0

    runner = ScheduleRunner([ServiceA(), ServiceB()])
    runner.start()

    # Both distinct providers must be scheduled; neither is silently overwritten/dropped.
    assert len(runner.registry.list()) == 2

    await asyncio.sleep(0.05)
    await runner.stop()

    assert ServiceA.runs > 0
    assert ServiceB.runs > 0
