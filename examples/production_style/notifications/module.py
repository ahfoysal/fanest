from fanest import Module

from examples.production_style.notifications.controller import OperationsController
from examples.production_style.notifications.listeners import UserLifecycleListener
from examples.production_style.notifications.processor import NotificationProcessor
from examples.production_style.notifications.service import NotificationsService


@Module(
    controllers=[OperationsController],
    providers=[NotificationsService, NotificationProcessor, UserLifecycleListener],
    exports=[NotificationsService],
)
class NotificationsModule:
    pass
