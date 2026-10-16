"""
Completes the loop SMTPOutreachProvider starts: since outreach is sent from
a real mailbox the founder owns, replies land in that same mailbox's INBOX.
This polls it via IMAP (a standard protocol every mailbox provider supports,
including Gmail/Google Workspace with an app password) -- no paid inbound-
parsing API, no webhook infrastructure to stand up.

Matching a reply to a prospect is by From-address: the reply arrives FROM
the prospect's own email TO the sending mailbox, and prospects.contact_email
is already the value we sent to. This is simpler and more robust than
Message-ID threading (which some mail clients mangle) and needs no extra
state to track. IMAP's own \\Seen flag is the de-duplication mechanism --
each poll only fetches UNSEEN messages and marks them seen once processed,
so a message is never double-counted even if two poll cycles overlap.
"""
from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass
from email.header import decode_header
from typing import Optional, Protocol


@dataclass
class InboundReply:
    from_email: str
    subject: str
    body: str
    imap_uid: str
    message_id: str = ""      # the RFC 5322 Message-ID header -- the idempotency key for at-least-once processing


class ImapClient(Protocol):
    """The subset of imaplib.IMAP4_SSL's interface this module needs --
    narrow on purpose, so a fake implementation for tests is trivial and
    can't accidentally rely on real-connection behavior."""

    def login(self, user: str, password: str): ...
    def select(self, mailbox: str): ...
    def search(self, charset, *criteria): ...
    def fetch(self, message_set: str, message_parts: str): ...
    def store(self, message_set: str, command: str, flags: str): ...
    def logout(self): ...


class ImapInboundChecker:
    """Real IMAP polling, real network connection -- this sandbox's network
    egress cannot reach an arbitrary mailbox provider, so this has been
    tested against a fake ImapClient with response shapes matching real
    imaplib behavior (tests/test_imap_inbound.py), never live. See
    docs/ENVIRONMENT.md for a smoke-test once running locally."""

    def __init__(self, *, host: str, username: str, password: str, mailbox: str = "INBOX",
                 imap_client_factory=None):
        self._host = host
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._client_factory = imap_client_factory or self._verified_client


    @staticmethod
    def _verified_client(host: str):
        """IMAPS with certificate chain + hostname verification (imaplib's default context verifies neither)."""
        from packages.core.tlsconfig import client_context
        return imaplib.IMAP4_SSL(host, ssl_context=client_context())

    def fetch_unread_replies(self, *, mark_seen: bool = True) -> list[InboundReply]:
        """mark_seen=True (default) flags each message \\Seen as it is fetched -- simple, but a crash
        between fetch and task creation would lose the reply. Production processing
        (packages/core/inbound.py) passes mark_seen=False, creates the tasks, THEN calls
        mark_seen(uids): at-least-once delivery, made safe by Message-ID de-duplication."""
        client = self._client_factory(self._host)
        replies: list[InboundReply] = []
        try:
            client.login(self._username, self._password)
            client.select(self._mailbox)
            status, data = client.search(None, "UNSEEN")
            if status != "OK":
                return []
            uids = data[0].split() if data and data[0] else []

            for uid in uids:
                uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
                fetch_status, fetch_data = client.fetch(uid_str, "(RFC822)")
                if fetch_status != "OK" or not fetch_data or not fetch_data[0]:
                    continue
                raw_bytes = fetch_data[0][1]
                parsed = email.message_from_bytes(raw_bytes)
                replies.append(InboundReply(
                    from_email=_extract_email_address(parsed.get("From", "")),
                    subject=_decode_header_value(parsed.get("Subject", "")),
                    body=_extract_plain_text_body(parsed),
                    imap_uid=uid_str,
                    message_id=(parsed.get("Message-ID", "") or "").strip(),
                ))
                if mark_seen:
                    client.store(uid_str, "+FLAGS", "\\Seen")
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return replies

    def mark_seen(self, uids: list[str]) -> None:
        if not uids:
            return
        client = self._client_factory(self._host)
        try:
            client.login(self._username, self._password)
            client.select(self._mailbox)
            for uid in uids:
                client.store(uid, "+FLAGS", "\\Seen")
        finally:
            try:
                client.logout()
            except Exception:
                pass


def _extract_email_address(from_header: str) -> str:
    # Handles both "Name <addr@x.com>" and a bare "addr@x.com".
    if "<" in from_header and ">" in from_header:
        return from_header.split("<", 1)[1].split(">", 1)[0].strip().lower()
    return from_header.strip().lower()


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded += part.decode(encoding or "utf-8", errors="replace")
        else:
            decoded += part
    return decoded


def _extract_plain_text_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        return ""
    charset = msg.get_content_charset() or "utf-8"
    payload = msg.get_payload(decode=True)
    return payload.decode(charset, errors="replace") if payload else ""
