from typing import Any

from fanest import Injectable
from fanest.mailer import MailerService


@Injectable()
class NotificationsService:
    def __init__(self, mailer: MailerService):
        self.mailer = mailer
        self.audit_log: list[dict[str, Any]] = []

    async def send_welcome_email(self, user: dict[str, Any]):
        message = await self.mailer.send_async(
            to=user["email"],
            subject=f"Welcome, {user['name']}",
            template="welcome",
            context={"name": user["name"]},
        )
        self.audit_log.append(
            {
                "kind": "email.sent",
                "to": user["email"],
                "subject": message.subject,
            }
        )
        return message

    def record_event(self, kind: str, payload: dict[str, Any]) -> None:
        self.audit_log.append({"kind": kind, "payload": payload})

    def recent(self) -> list[dict[str, Any]]:
        return list(self.audit_log[-25:])
