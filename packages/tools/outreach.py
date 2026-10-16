"""
Interface for the real outreach provider (Smartlead/Instantly-class API --
see docs/ARCHITECTURE.md Section 7 for why a dedicated provider, not
self-hosted SMTP, is the *eventual* answer at real scale). LoggingOutreachProvider
is the $0-cost, no-credentials-required default: it never contacts anyone,
it only records what a real send would have looked like.

SMTPOutreachProvider is a second, real $0-cost option: it sends through the
founder's own domain mailbox via standard SMTP, not through a transactional
ESP (Resend, SendGrid, Mailgun, Postmark, and similar all explicitly
prohibit cold outreach in their Acceptable Use Policies -- confirmed against
Resend's actual AUP, which XFlow already uses for transactional email: using
that same account for cold prospecting risks losing it entirely, including
XFlow's password-reset/receipt email). CAN-SPAM makes US B2B cold email
legal without prior consent, provided every message has a truthful sender,
a non-deceptive subject, a physical address, and a working opt-out --
SalesAgent (packages/agents/departments/sales.py) enforces the address and
opt-out language before a send is ever attempted; this provider is only
responsible for actually transmitting the message and setting the
List-Unsubscribe header.

Swapping in a real dedicated cold-outreach provider (Smartlead/Instantly)
later is a config change and a new class -- SalesAgent's code doesn't change
either way.
"""
from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage


@dataclass
class SendResult:
    sent: bool
    provider_message_id: str | None
    note: str


class OutreachProvider(ABC):
    @abstractmethod
    def send(self, *, to_email: str, subject: str, body: str) -> SendResult:
        raise NotImplementedError


class LoggingOutreachProvider(OutreachProvider):
    """Never sends anything. Exists so the Sales Agent's send tool can be built,
    permission-checked, approval-gated, and audit-logged for real -- with the
    one thing that would cost money or reach an actual human left inert until
    a real provider is actually configured."""

    def send(self, *, to_email: str, subject: str, body: str) -> SendResult:
        return SendResult(
            sent=False,
            provider_message_id=None,
            note=f"LoggingOutreachProvider: would have sent to {to_email!r} (subject={subject!r}) -- no real provider configured.",
        )


class DailySendCapExceeded(Exception):
    pass


class SMTPOutreachProvider(OutreachProvider):
    """Real sends via a standard SMTP mailbox the founder already owns (their
    domain's own email account -- not a new paid service, and not a
    transactional ESP whose terms forbid this use case). Deliberately
    conservative by default:

    - A hard daily send cap (default 30/day) -- protects the sending
      domain's reputation. Low volume is not a limitation for a "10
      customers in 30 days" goal; it's the appropriate pace for a brand-new
      sending identity with no warmup history.
    - Sets the List-Unsubscribe header (RFC 8058) alongside the in-body
      opt-out text SalesAgent already requires -- most mail clients render a
      one-click unsubscribe from this header, which measurably reduces spam
      complaints (and complaint rate is exactly what gets a sending domain
      blocklisted).
    - The SMTP client is injectable (smtp_client_factory) so tests never
      open a real network connection -- consistent with every other tool in
      this codebase being tested via mocks/stubs, not live calls.
    """

    def __init__(self, *, host: str, port: int, username: str, password: str,
                 from_email: str, from_name: str = "", use_tls: bool = True,
                 daily_send_cap: int = 30, smtp_client_factory=None, ssl_context=None):
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._from_email = from_email
        self._from_name = from_name
        self._use_tls = use_tls
        self._daily_send_cap = daily_send_cap
        self._smtp_client_factory = smtp_client_factory or (lambda host, port: smtplib.SMTP(host, port, timeout=15))
        self._ssl_context = ssl_context            # None => built lazily from settings (verified by default)
        self._sent_today = 0
        self._sent_today_date = date.today()

    def _check_and_increment_cap(self) -> None:
        today = date.today()
        if today != self._sent_today_date:
            self._sent_today = 0
            self._sent_today_date = today
        if self._sent_today >= self._daily_send_cap:
            raise DailySendCapExceeded(
                f"Daily send cap ({self._daily_send_cap}) reached -- refusing to send more today. "
                "This protects the sending domain's reputation; raise the cap deliberately once you "
                "have real deliverability headroom, not by default."
            )
        self._sent_today += 1

    def send(self, *, to_email: str, subject: str, body: str) -> SendResult:
        self._check_and_increment_cap()

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{self._from_name} <{self._from_email}>" if self._from_name else self._from_email
        msg["To"] = to_email
        # RFC 8058 one-click unsubscribe -- mailto fallback since there's no real
        # unsubscribe URL/webhook receiver yet (tracked in STATUS.md).
        msg["List-Unsubscribe"] = f"<mailto:{self._from_email}?subject=unsubscribe>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content(body)

        smtp = self._smtp_client_factory(self._host, self._port)
        try:
            if self._use_tls:
                from packages.core.tlsconfig import client_context
                smtp.starttls(context=self._ssl_context or client_context())     # verifies certificate chain AND hostname
            smtp.login(self._username, self._password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass          # best-effort cleanup: never let it mask the REAL error (e.g. a failed TLS handshake)

        return SendResult(sent=True, provider_message_id=msg.get("Message-Id"), note=f"Sent via SMTP ({self._host}) to {to_email!r}.")
