import smtplib
import re
import inspect
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Protocol

from fanest import Inject, Injectable, Module, use_value
from fanest.core.providers import token

MAILER_OPTIONS = token("MAILER_OPTIONS")


@dataclass(frozen=True)
class MailAttachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class MailMessage:
    to: str | list[str]
    subject: str
    text: str | None = None
    html: str | None = None
    sender: str | None = None
    cc: str | list[str] | None = None
    bcc: str | list[str] | None = None
    reply_to: str | None = None
    attachments: list[str | Path | MailAttachment] | None = None


class MailerTransport(Protocol):
    def send(self, message: MailMessage) -> Any: ...


class SmtpMailerTransport:
    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def send(self, message: MailMessage) -> None:
        email = self.build_email(message)
        with smtplib.SMTP(self.options["host"], self.options.get("port", 25)) as client:
            if self.options.get("username"):
                client.login(self.options["username"], self.options.get("password", ""))
            client.send_message(email, to_addrs=_envelope_recipients(message))

    def build_email(self, message: MailMessage) -> EmailMessage:
        email = EmailMessage()
        email["Subject"] = message.subject
        email["From"] = message.sender or self.options.get("from") or ""
        email["To"] = ", ".join(_recipients(message.to))
        if message.cc:
            email["Cc"] = ", ".join(_recipients(message.cc))
        if message.reply_to:
            email["Reply-To"] = message.reply_to
        email["Date"] = formatdate(localtime=True)
        email["Message-ID"] = make_msgid()
        if message.text:
            email.set_content(message.text)
        if message.html:
            email.add_alternative(message.html, subtype="html")
        for attachment in message.attachments or []:
            normalized = _attachment(attachment)
            maintype, _, subtype = normalized.content_type.partition("/")
            email.add_attachment(
                normalized.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=normalized.filename,
            )
        return email


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
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        reply_to: str | None = None,
        attachments: list[str | Path | MailAttachment] | None = None,
    ) -> MailMessage:
        message = self.create_message(
            to=to,
            subject=subject,
            text=text,
            html=html,
            template=template,
            context=context,
            sender=sender,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            attachments=attachments,
        )
        self._record(message)
        result = self._send_transport(message)
        if inspect.isawaitable(result):
            # A sync send() cannot await an async transport; silently dropping the
            # coroutine means the mail is never delivered. Fail loudly and steer
            # callers to send_async() instead of leaking an un-awaited coroutine.
            if inspect.iscoroutine(result):
                result.close()
            raise RuntimeError(
                "Transport.send() returned an awaitable; use MailerService.send_async() "
                "for asynchronous transports."
            )
        return message

    async def send_async(
        self,
        *,
        to: str | list[str],
        subject: str,
        text: str | None = None,
        html: str | None = None,
        template: str | None = None,
        context: dict[str, Any] | None = None,
        sender: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        reply_to: str | None = None,
        attachments: list[str | Path | MailAttachment] | None = None,
    ) -> MailMessage:
        message = self.create_message(
            to=to,
            subject=subject,
            text=text,
            html=html,
            template=template,
            context=context,
            sender=sender,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            attachments=attachments,
        )
        self._record(message)
        result = self._send_transport(message)
        if inspect.isawaitable(result):
            await result
        return message

    def create_message(
        self,
        *,
        to: str | list[str],
        subject: str,
        text: str | None = None,
        html: str | None = None,
        template: str | None = None,
        context: dict[str, Any] | None = None,
        sender: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        reply_to: str | None = None,
        attachments: list[str | Path | MailAttachment] | None = None,
    ) -> MailMessage:
        if template is not None:
            text = self.render_template(template, context or {})
        return MailMessage(
            to=to,
            subject=subject,
            text=text,
            html=html,
            sender=sender or self.options.get("default_from"),
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            attachments=attachments,
        )

    def _record(self, message: MailMessage) -> None:
        if self.options.get("outbox", True):
            self.outbox.append(message)

    def _send_transport(self, message: MailMessage) -> Any:
        transport = self.options.get("transport")
        if transport is not None:
            return transport.send(message)
        if self.options.get("smtp"):
            return SmtpMailerTransport(self.options["smtp"]).send(message)
        return None

    def render_template(self, template: str, context: dict[str, Any]) -> str:
        templates = self.options.get("templates", {})
        source = templates.get(template, template)

        def replace(match: re.Match[str]) -> str:
            return str(context.get(match.group("key"), ""))

        return re.sub(r"{{\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*}}", replace, source)


class MailerModule:
    @staticmethod
    def for_root(is_global: bool = False, **options: Any) -> type:
        @Module(
            providers=[use_value(MAILER_OPTIONS, options), MailerService],
            exports=[MailerService],
            global_module=is_global,
        )
        class DynamicMailerModule:
            pass

        return DynamicMailerModule


def _recipients(value: str | list[str]) -> list[str]:
    return value if isinstance(value, list) else [value]


def _envelope_recipients(message: MailMessage) -> list[str]:
    recipients = _recipients(message.to)
    if message.cc:
        recipients.extend(_recipients(message.cc))
    if message.bcc:
        recipients.extend(_recipients(message.bcc))
    return recipients


def _attachment(value: str | Path | MailAttachment) -> MailAttachment:
    if isinstance(value, MailAttachment):
        return value
    path = Path(value)
    return MailAttachment(filename=path.name, content=path.read_bytes())
