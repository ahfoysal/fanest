import smtplib
import re
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token

MAILER_OPTIONS = token("MAILER_OPTIONS")


@dataclass(frozen=True)
class MailMessage:
    to: str | list[str]
    subject: str
    text: str | None = None
    html: str | None = None
    sender: str | None = None


@Injectable()
class MailerService:
    def __init__(self, options: dict[str, Any] = Inject(MAILER_OPTIONS)):
        self.options = options
        self.outbox: list[MailMessage] = []

    def send(
        self,
        *,
        to: str | list[str],
        subject: str,
        text: str | None = None,
        html: str | None = None,
        template: str | None = None,
        context: dict[str, Any] | None = None,
        sender: str | None = None,
    ) -> MailMessage:
        if template is not None:
            text = self.render_template(template, context or {})
        message = MailMessage(
            to=to,
            subject=subject,
            text=text,
            html=html,
            sender=sender or self.options.get("default_from"),
        )
        self.outbox.append(message)
        if self.options.get("smtp"):
            self._send_smtp(message)
        return message

    def render_template(self, template: str, context: dict[str, Any]) -> str:
        templates = self.options.get("templates", {})
        source = templates.get(template, template)

        def replace(match: re.Match[str]) -> str:
            return str(context.get(match.group("key"), ""))

        return re.sub(r"{{\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*}}", replace, source)

    def _send_smtp(self, message: MailMessage) -> None:
        smtp_options = self.options["smtp"]
        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = message.sender or smtp_options.get("from") or ""
        recipients = message.to if isinstance(message.to, list) else [message.to]
        email["To"] = ", ".join(recipients)
        if message.text:
            email.set_content(message.text)
        if message.html:
            email.add_alternative(message.html, subtype="html")
        with smtplib.SMTP(smtp_options["host"], smtp_options.get("port", 25)) as client:
            if smtp_options.get("username"):
                client.login(smtp_options["username"], smtp_options.get("password", ""))
            client.send_message(email)


class MailerModule:
    @staticmethod
    def for_root(**options: Any) -> type:
        @Module(providers=[use_value(MAILER_OPTIONS, options), MailerService], exports=[MailerService])
        class DynamicMailerModule:
            pass

        return DynamicMailerModule
