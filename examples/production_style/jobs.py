from fanest import Injectable
from fanest.queues import QueueService
from fanest.schedule import CronExpression, CronJob, Interval, Timeout


@Injectable()
class MaintenanceJobs:
    def __init__(self, queue: QueueService):
        self.queue = queue
        self.snapshots: list[dict[str, int]] = []

    @Interval(3600, name="queue-snapshot")
    async def queue_snapshot(self):
        stats = self.queue.stats("notifications")
        self.snapshots.append(
            {
                "waiting": stats.waiting,
                "completed": stats.completed,
                "failed": stats.failed,
            }
        )

    @Timeout(5, name="startup-health-marker")
    async def startup_health_marker(self):
        self.snapshots.append({"waiting": 0, "completed": 0, "failed": 0})

    @CronJob(CronExpression.EVERY_DAY_AT_MIDNIGHT, name="disabled-archive", disabled=True)
    async def disabled_archive_job(self):
        return None
