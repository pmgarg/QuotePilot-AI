"""Outbound/inbound mail providers behind one protocol (S2-F06).

- simulator: default; stores messages in memory/DB-free list per process and can inject synthetic inbound replies.
- smtp: real SMTP via smtplib when configured (optional; no credentials in repo).
- gmail / msgraph: interfaces documented, not implemented in this release (NotConfigured) — see OPERATIONS_RUNBOOK.
Providers never decide *whether* to send; the service layer enforces consent, opt-out, rate limits and audit.
"""

from __future__ import annotations

import email.utils
import re
import smtplib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol

from app.db.base import utcnow

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class ProviderError(Exception):
    """Transient (retryable) or permanent provider failure."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ProviderNotConfigured(ProviderError):
    def __init__(self, provider: str) -> None:
        super().__init__(f"Provider '{provider}' is not configured for this organisation", retryable=False)


@dataclass(frozen=True)
class OutboundMessage:
    to_email: str
    subject: str
    body_text: str
    from_name: str
    reply_to: str | None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    provider: str
    provider_message_id: str
    accepted_at: datetime


@dataclass(frozen=True)
class InboundMessage:
    provider: str
    provider_message_id: str
    from_email: str
    subject: str
    snippet: str
    received_at: datetime
    in_reply_to: str | None = None


class MailProvider(Protocol):
    name: str

    def send(self, message: OutboundMessage) -> SendResult: ...
    def fetch_inbound(self, cursor: str | None) -> tuple[list[InboundMessage], str | None]: ...


def valid_email(address: str | None) -> bool:
    return bool(address) and bool(EMAIL_RE.match(address or "")) and len(address or "") <= 320


class SimulatorProvider:
    """Deterministic fake provider. Outbox/inbox live in process memory; tests and demos can script failures."""

    name = "simulator"

    def __init__(self) -> None:
        self.outbox: list[tuple[OutboundMessage, SendResult]] = []
        self.inbox: list[InboundMessage] = []
        self.fail_next: list[ProviderError] = []

    def send(self, message: OutboundMessage) -> SendResult:
        if self.fail_next:
            raise self.fail_next.pop(0)
        if not valid_email(message.to_email):
            raise ProviderError("recipient address rejected", retryable=False, status=400)
        result = SendResult(self.name, f"sim-{uuid.uuid4()}", utcnow())
        self.outbox.append((message, result))
        return result

    def inject_inbound(self, msg: InboundMessage) -> None:
        self.inbox.append(msg)

    def fetch_inbound(self, cursor: str | None) -> tuple[list[InboundMessage], str | None]:
        start = int(cursor) if cursor else 0
        batch = self.inbox[start:]
        return batch, str(len(self.inbox))


class SMTPProvider:
    name = "smtp"

    def __init__(
        self, host: str, port: int, username: str | None, password: str | None, use_tls: bool, from_email: str
    ) -> None:
        self.host, self.port, self.username, self.password, self.use_tls, self.from_email = (
            host,
            port,
            username,
            password,
            use_tls,
            from_email,
        )

    def send(self, message: OutboundMessage) -> SendResult:
        if not valid_email(message.to_email):
            raise ProviderError("recipient address rejected", retryable=False, status=400)
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((message.from_name, self.from_email))
        msg["To"] = message.to_email
        msg["Subject"] = message.subject.replace("\r", " ").replace("\n", " ")[:300]
        msg["Message-ID"] = email.utils.make_msgid()
        if message.reply_to and valid_email(message.reply_to):
            msg["Reply-To"] = message.reply_to
        for k, v in message.headers.items():
            msg[k] = v.replace("\r", " ").replace("\n", " ")
        msg.set_content(message.body_text)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError) as exc:
            raise ProviderError(f"SMTP unavailable ({type(exc).__name__})", retryable=True) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise ProviderError("recipient refused by server", retryable=False, status=550) from exc
        except smtplib.SMTPResponseException as exc:
            retry = 400 <= exc.smtp_code < 500
            raise ProviderError(f"SMTP error {exc.smtp_code}", retryable=retry, status=exc.smtp_code) from exc
        return SendResult(self.name, str(msg["Message-ID"]), utcnow())

    def fetch_inbound(self, cursor: str | None) -> tuple[list[InboundMessage], str | None]:
        return [], cursor  # SMTP is outbound-only; inbound sync needs Gmail/Graph/IMAP (documented)


class NotImplementedProvider:
    """Gmail / Microsoft Graph: OAuth setup documented; adapters intentionally not shipped untested."""

    def __init__(self, name: str) -> None:
        self.name = name

    def send(self, message: OutboundMessage) -> SendResult:
        raise ProviderNotConfigured(self.name)

    def fetch_inbound(self, cursor: str | None) -> tuple[list[InboundMessage], str | None]:
        raise ProviderNotConfigured(self.name)


_simulators: dict[str, SimulatorProvider] = {}


def simulator_for(org_id: uuid.UUID | str) -> SimulatorProvider:
    key = str(org_id)
    if key not in _simulators:
        _simulators[key] = SimulatorProvider()
    return _simulators[key]


def reset_simulators() -> None:
    _simulators.clear()
