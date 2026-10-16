"""The hard qualification gate: every rule, and that nothing is ever 'passed' to fill a quota."""
from __future__ import annotations

import pytest

from packages.core import qualification as q
from packages.core.domains import StaticDomainChecker

POLICY = q.QualificationPolicy.from_icp({"buyer_titles": ["Owner"], "qualification": {"min_score": 0.6, "required_keywords_any": ["plumb"]}})
OK_RESEARCH = {"status": "ok", "text": "family plumbing company in town " * 20, "hook": "Family plumbing company"}


def ingest(email, website="https://acme.com", title="Owner", policy=POLICY, checker=None):
    return q.finalize("ingest", q.evaluate_ingest(q.Candidate(email, website, title, "Acme"), policy, checker or StaticDomainChecker()), policy, require_score=False)


def test_policy_absent_means_legacy_behaviour():
    assert q.QualificationPolicy.from_icp({"industries": ["x"]}) is None


def test_good_prospect_passes_ingest():
    r = ingest("pat@acme.com")
    assert r.passed and r.failed == [] and r.score >= 0.7


@pytest.mark.parametrize("email,failed", [
    ("not-an-email", "email_syntax"), ("a..b@acme.com", "email_syntax"),
    ("pat@acme.invalid", "email_not_placeholder"), ("pat@example.com", "email_not_placeholder"),
    ("noreply@acme.com", "email_local_part_ok"), ("postmaster@acme.com", "email_local_part_ok"),
    ("pat@gmail.com", "email_domain_allowed"), ("pat@city.gov", "email_domain_allowed"),
])
def test_hard_email_failures(email, failed):
    r = ingest(email)
    assert not r.passed and failed in r.failed


def test_missing_email_fails_when_required():
    assert not ingest(None).passed and "email_present" in ingest(None).failed


def test_dns_invalid_and_unknown_domains_both_fail_closed():
    assert "email_domain_mx" in ingest("pat@acme.com", checker=StaticDomainChecker({"acme.com": "invalid"})).failed
    unknown = ingest("pat@acme.com", checker=StaticDomainChecker({"acme.com": "unknown"}))
    assert not unknown.passed and "email_domain_mx" in unknown.failed       # cannot verify => do not send


def test_role_address_policy():
    strict = q.QualificationPolicy(allow_role_emails=False)
    assert "email_local_part_ok" in ingest("info@acme.com", policy=strict).failed
    assert "email_local_part_ok" not in ingest("info@acme.com").failed       # allowed by the default policy, just scores lower
    assert ingest("info@acme.com").score < ingest("pat@acme.com").score


def test_free_mail_allowed_only_when_policy_says_so():
    assert "email_domain_allowed" in ingest("pat@gmail.com").failed
    assert "email_domain_allowed" not in ingest("pat@gmail.com", policy=q.QualificationPolicy(allow_free_mail=True)).failed


def test_blocked_domains_and_tlds_from_policy():
    policy = q.QualificationPolicy(blocked_domains=["bigchain.com"])
    assert "email_domain_allowed" in ingest("pat@bigchain.com", policy=policy).failed


def test_website_required_by_default():
    assert "website_present" in ingest("pat@acme.com", website=None).failed


def test_research_pass_and_merge_scores_together():
    final = q.merge_stages(ingest("pat@acme.com").to_json(), q.evaluate_research(OK_RESEARCH, POLICY), POLICY)
    assert final.passed and final.score >= 0.6 and final.stage == "final"


@pytest.mark.parametrize("research,failed", [
    ({"status": "unreachable"}, "site_reachable"),
    ({"status": "blocked_by_robots"}, "site_reachable"),
    ({"status": "ok", "text": "this domain is for sale", "hook": "x"}, "site_is_live_business"),
    ({"status": "ok", "text": "we sell shoes and handbags online " * 10, "hook": "x"}, "industry_keyword_match"),
    ({"status": "ok", "text": "plumbing " * 30, "hook": None}, "personalization_hook"),
])
def test_research_hard_failures(research, failed):
    checks = q.evaluate_research(research, POLICY)
    assert failed in [c.name for c in checks if c.hard and not c.passed]


def test_excluded_keywords_disqualify():
    policy = q.QualificationPolicy(exclude_keywords=["franchise"], required_keywords_any=["plumb"])
    checks = q.evaluate_research({"status": "ok", "text": "plumbing franchise opportunities " * 10, "hook": "x"}, policy)
    assert "no_excluded_keywords" in [c.name for c in checks if not c.passed]


def test_score_below_min_fails_even_when_every_hard_check_passes():
    """A prospect that clears the hard checks but is a weak fit is NOT sent to just to fill the day."""
    policy = q.QualificationPolicy(min_score=0.95, required_keywords_any=["plumb"])
    base = ingest("info@acme.com", website="https://other.com", title="Receptionist", policy=policy)
    final = q.merge_stages(base.to_json(), q.evaluate_research(OK_RESEARCH, policy), policy)
    assert not [c for c in final.checks if c.hard and not c.passed] and not final.passed and final.score < 0.95


def test_a_failed_ingest_can_never_pass_after_research():
    bad = ingest("noreply@acme.com")
    final = q.merge_stages(bad.to_json(), q.evaluate_research(OK_RESEARCH, POLICY), POLICY)
    assert not final.passed and "email_local_part_ok" in final.failed


def test_result_json_is_explainable():
    j = ingest("pat@acme.invalid").to_json()
    assert j["passed"] is False and j["failed"] and all({"name", "passed", "detail"} <= set(c) for c in j["checks"])
