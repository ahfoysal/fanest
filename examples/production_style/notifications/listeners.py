from fanest import Injectable
from fanest.events import OnEvent

from examples.production_style.notifications.service import NotificationsService


@Injectable()
class UserLifecycleListener:
    def __init__(self, notifications: NotificationsService):
        self.notifications = notifications

    @OnEvent("user.*", priority=10)
    def record_user_event(self, payload):
        self.notifications.record_event("user.lifecycle", payload)

    @OnEvent("user.created")
    def record_signup_metric(self, payload):
        self.notifications.record_event("metric.signup", {"user_id": payload["id"]})
