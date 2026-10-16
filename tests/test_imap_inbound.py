"""
FakeImapClient mimics imaplib.IMAP4_SSL's response shapes exactly (status
strings, byte-encoded UID lists, (header, body) tuples from fetch) -- no
real network connection to any mailbox provider happens in this suite. This
sandbox's network egress can't reach one anyway; see the module docstring
in packages/tools/imap_inbound.py.
"""
from __future__ import annotations

from email.message import EmailMessage

from packages.tools.imap_inbound import ImapInboundChecker


def _raw_email(*, from_addr: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "sales@sparserest.com"
    msg["Subject"] = subject
    msg.set_content(body)
    return bytes(msg)


class FakeImapClient:
    def __init__(self, unseen_uids: list[bytes], messages: dict[str, bytes]):
        self._unseen_uids = unseen_uids
        self._messages = messages  # uid_str -> raw bytes
        self.logged_in_as = None
        self.selected_mailbox = None
        self.stored_flags = []
        self.logged_out = False

    def login(self, user, password):
        self.logged_in_as = (user, password)

    def select(self, mailbox):
        self.selected_mailbox = mailbox

    def search(self, charset, *criteria):
        return "OK", [b" ".join(self._unseen_uids)]

    def fetch(self, message_set, message_parts):
        raw = self._messages.get(message_set)
        if raw is None:
            return "NO", []
        return "OK", [(f"{message_set} (RFC822 {{{len(raw)}}}".encode(), raw)]

    def store(self, message_set, command, flags):
        self.stored_flags.append((message_set, command, flags))
        return "OK", []

    def logout(self):
        self.logged_out = True


def test_fetches_and_parses_a_real_shaped_unread_reply():
    raw = _raw_email(from_addr="Pat Owner <owner@prospect.invalid>", subject="Re: Quick question", body="Sure, let's talk this week.")
    fake = FakeImapClient(unseen_uids=[b"1"], messages={"1": raw})
    checker = ImapInboundChecker(host="imap.example.com", username="sales@sparserest.com", password="app-password", imap_client_factory=lambda host: fake)

    replies = checker.fetch_unread_replies()

    assert len(replies) == 1
    assert replies[0].from_email == "owner@prospect.invalid"  # angle-bracket address extracted, lowercased
    assert "Re: Quick question" in replies[0].subject
    assert "let's talk this week" in replies[0].body
    assert fake.logged_in_as == ("sales@sparserest.com", "app-password")
    assert fake.selected_mailbox == "INBOX"


def test_marks_fetched_messages_as_seen():
    raw = _raw_email(from_addr="owner@prospect.invalid", subject="Re: hi", body="ok")
    fake = FakeImapClient(unseen_uids=[b"5"], messages={"5": raw})
    checker = ImapInboundChecker(host="imap.example.com", username="u", password="p", imap_client_factory=lambda host: fake)

    checker.fetch_unread_replies()

    assert fake.stored_flags == [("5", "+FLAGS", "\\Seen")]


def test_no_unread_messages_returns_empty_list():
    fake = FakeImapClient(unseen_uids=[], messages={})
    checker = ImapInboundChecker(host="imap.example.com", username="u", password="p", imap_client_factory=lambda host: fake)
    assert checker.fetch_unread_replies() == []


def test_always_logs_out_even_if_something_fails_midway():
    class ExplodingClient(FakeImapClient):
        def fetch(self, message_set, message_parts):
            raise RuntimeError("connection dropped")

    fake = ExplodingClient(unseen_uids=[b"1"], messages={"1": b"irrelevant"})
    checker = ImapInboundChecker(host="imap.example.com", username="u", password="p", imap_client_factory=lambda host: fake)

    try:
        checker.fetch_unread_replies()
    except RuntimeError:
        pass
    assert fake.logged_out is True, "must always log out, even when a mid-loop fetch fails"


def test_handles_bare_from_address_without_angle_brackets():
    raw = _raw_email(from_addr="owner@prospect.invalid", subject="hi", body="hello")
    fake = FakeImapClient(unseen_uids=[b"1"], messages={"1": raw})
    checker = ImapInboundChecker(host="imap.example.com", username="u", password="p", imap_client_factory=lambda host: fake)
    replies = checker.fetch_unread_replies()
    assert replies[0].from_email == "owner@prospect.invalid"
