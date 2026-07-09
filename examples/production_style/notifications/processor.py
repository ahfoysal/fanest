from fanest.queues import Process, Processor

from examples.production_style.notifications.service import NotificationsService


@Processor("notifications")
class NotificationProcessor:
    def __init__(self, notifications: NotificationsService):
        self.notifications = notifications

    @Process("send_email")
    async def send_email(self, job):
        template = job.data.get("template")
        if template == "welcome":
            await self.notifications.send_welcome_email(job.data["user"])
