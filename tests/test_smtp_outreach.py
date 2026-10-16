"""
SMTPOutreachProvider tested against a fake SMTP client -- consistent with
every other tool in this codebase (Overpass/enrichment included) being
tested via injected fakes, never a live network call. This proves the send
logic, the List-Unsubscribe header, and the daily cap actually work; it
cannot prove real deliverability, which no automated test can.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from packages.tools.outreach import DailySendCapExceeded, SMTPOutreachProvider


class FakeSMTP:
    """Records what would have happened on the wire, opens no real connection."""

    instances = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in_as = None
        self.sent_messages = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, username, password):
        self.logged_in_as = (username, password)

    def send_message(self, msg):
        self.sent_messages.append(msg)

    def quit(self):
        self.quit_called = True


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSMTP.instances.clear()
    yield
    FakeSMTP.instances.clear()


def make_provider(**overrides):
    defaults = dict(
        host="smtp.example.com", port=587, username="sales@sparserest.com", password="app-password",
        from_email="sales@sparserest.com", from_name="SparseReST", daily_send_cap=3,
        smtp_client_factory=lambda host, port: FakeSMTP(host, port),
    )
    defaults.update(overrides)
    return SMTPOutreachProvider(**defaults)


def test_send_uses_tls_login_and_sends_the_message():
    provider = make_provider()
    result = provider.send(to_email="owner@prospect.invalid", subject="Quick question", body="Hi there")

    assert result.sent is True
    fake = FakeSMTP.instances[0]
    assert fake.started_tls is True
    assert fake.logged_in_as == ("sales@sparserest.com", "app-password")
    assert len(fake.sent_messages) == 1
    assert fake.quit_called is True


def test_send_sets_list_unsubscribe_header_for_one_click_opt_out():
    provider = make_provider()
    provider.send(to_email="owner@prospect.invalid", subject="Quick question", body="Hi there")

    sent_msg = FakeSMTP.instances[0].sent_messages[0]
    assert "List-Unsubscribe" in sent_msg
    assert "mailto:sales@sparserest.com" in sent_msg["List-Unsubscribe"]
    assert sent_msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_daily_send_cap_is_enforced():
    provider = make_provider(daily_send_cap=2)
    provider.send(to_email="a@prospect.invalid", subject="s", body="b")
    provider.send(to_email="b@prospect.invalid", subject="s", body="b")
    with pytest.raises(DailySendCapExceeded):
        provider.send(to_email="c@prospect.invalid", subject="s", body="b")

    # Confirmed the third send never even opened a connection -- the cap is
    # checked before any SMTP interaction, not after a wasted connection.
    assert len(FakeSMTP.instances) == 2


def test_daily_send_cap_resets_on_a_new_day():
    provider = make_provider(daily_send_cap=1)
    provider.send(to_email="a@prospect.invalid", subject="s", body="b")
    with pytest.raises(DailySendCapExceeded):
        provider.send(to_email="b@prospect.invalid", subject="s", body="b")

    # Simulate the next day rolling over.
    provider._sent_today_date = date.today() - timedelta(days=1)
    result = provider.send(to_email="c@prospect.invalid", subject="s", body="b")
    assert result.sent is True
