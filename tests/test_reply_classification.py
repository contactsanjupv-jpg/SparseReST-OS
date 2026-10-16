"""Reply classification regressions. A rejection must never be escalated as a hot lead."""
import pytest

from packages.agents.departments.sales import classify_reply_text as c

CASES = [
    ("Not interested", "not_interested"), ("not interested, thanks", "not_interested"), ("We're not interested at this time", "not_interested"),
    ("Not interested, please don't call me", "not_interested"), ("Not interested. Why do you keep emailing me?", "not_interested"),
    ("no thanks", "not_interested"), ("Thanks, we're all set", "not_interested"), ("We'll pass.", "not_interested"), ("Not a good fit for us", "not_interested"),
    ("Yes interested, call me tomorrow please", "high_intent"), ("Let's talk Thursday", "high_intent"), ("Sign me up", "high_intent"),
    ("I'm interested, send info", "interested"), ("Sounds good, tell me more", "interested"),
    ("Not right now, check back in Q2", "maybe"), ("Maybe later in the year", "maybe"), ("hmm", "maybe"),
    ("How much does it cost?", "question"), ("Does this integrate with QuickBooks?", "question"),
    ("Please unsubscribe me", "unsubscribe"), ("Stop emailing me", "unsubscribe"), ("Take me off your list", "unsubscribe"), ("Do not contact me again", "unsubscribe"),
    ("I am out of the office until Monday", "out_of_office"), ("Automatic reply: on vacation", "out_of_office"),
    ("I'm no longer with the company", "wrong_person"), ("You have the wrong person", "wrong_person"),
    ("We're already using something similar", "existing_customer"),
    ("This seems too expensive for us", "objection"),
]


@pytest.mark.parametrize("text,expected", CASES)
def test_classification(text, expected):
    assert c(text) == expected


def test_an_unsubscribe_always_beats_every_other_signal():
    assert c("I'm interested but please unsubscribe me, call me never") == "unsubscribe"
