import asyncio
import time
from datetime import datetime, timezone

import pytest

from fanest.schedule import CronExpression, CronJob, Interval
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
