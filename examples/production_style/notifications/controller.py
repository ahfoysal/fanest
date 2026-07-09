from fanest import Controller, Get
from fanest.mailer import MailerService
from fanest.queues import QueueService

from examples.production_style.notifications.service import NotificationsService


@Controller("ops")
class OperationsController:
    def __init__(
        self,
        notifications: NotificationsService,
        mailer: MailerService,
        queue: QueueService,
    ):
        self.notifications = notifications
        self.mailer = mailer
        self.queue = queue

    @Get("notifications")
    async def notifications_status(self):
        return {
            "audit": self.notifications.recent(),
            "mail_outbox": len(self.mailer.outbox),
            "queue": self.queue.stats("notifications").__dict__,
        }
