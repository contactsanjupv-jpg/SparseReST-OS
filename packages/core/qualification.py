"""
The HARD QUALIFICATION GATE.

A prospect is contacted only if it passes every hard check -- there is no quota anywhere in this
system, and nothing here ever relaxes a check to "fill the day". If 8 prospects qualify, 8 are
contacted. Each check records a human-readable reason, so a disqualified prospect is explainable and
a failing source/ICP shows up in the growth report instead of silently producing bad sends.

Two stages, both stored on `prospects.qualification`:
  1. ingest   -- what we know at discovery: a deliverable-looking contact email on a real business domain.
  2. research -- after reading the prospect's own website: it is a live, real, on-ICP business and there
                 is something specific and TRUE to say to them.
`passed` is True only when both are done and every hard check passed and the score meets `min_score`.

The policy lives in product_config.icp["qualification"] so it is the founder's decision, per product.
Without that block the legacy scoring in ProspectingAgent applies (kept so existing flows don't change);
the autopilot refuses to run without it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from packages.core.domains import DomainChecker

FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com",
}
BLOCKED_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "abuse", "privacy", "legal",
    "dmca", "security", "unsubscribe", "bounce", "bounces", "webmaster", "hostmaster", "root", "spam", "compliance",
}
ROLE_LOCAL_PARTS = {"info", "contact", "office", "hello", "sales", "support", "admin", "enquiries", "inquiries", "team", "service", "mail", "help"}
PLACEHOLDER_TLDS = {"invalid", "test", "example", "localhost", "local", "internal", "lan"}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com", "test.com", "yourdomain.com", "sentry.io"}
DEFAULT_BLOCKED_TLDS = {"gov", "mil", "edu"}
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@([A-Za-z0-9\-]{1,63}\.)+[A-Za-z]{2,24}$")
PARKED_MARKERS = ("this domain is for sale", "buy this domain", "domain is parked", "parked free", "domain may be for sale",
                  "this site can't be reached", "account suspended", "under construction", "coming soon")


@dataclass
class QualificationPolicy:
    min_score: float = 0.6
    require_email: bool = True
    require_mx: bool = True
    allow_role_emails: bool = True
    allow_free_mail: bool = False
    require_website: bool = True
    required_keywords_any: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    blocked_tlds: list[str] = field(default_factory=lambda: sorted(DEFAULT_BLOCKED_TLDS))
    require_personalization_hook: bool = True
    buyer_titles: list[str] = field(default_factory=list)

    @classmethod
    def from_icp(cls, icp: Optional[dict]) -> Optional["QualificationPolicy"]:
        """None when the ICP has no `qualification` block (legacy behaviour)."""
        block = (icp or {}).get("qualification")
        if not isinstance(block, dict):
            return None
        known = {f for f in cls.__dataclass_fields__}
        policy = cls(**{k: v for k, v in block.items() if k in known})
        policy.buyer_titles = list((icp or {}).get("buyer_titles") or [])
        policy.min_score = max(0.0, min(1.0, float(policy.min_score)))
        return policy


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    hard: bool = True
    weight: float = 0.0
    retryable: bool = False       # a failure that reflects a TEMPORARY inability to verify (e.g. DNS timed out), not a verdict


@dataclass
class Candidate:
    email: Optional[str]
    website: Optional[str]
    contact_title: Optional[str] = None
    company_name: str = ""


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower()


def site_domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    host = urlparse(url if "://" in url else f"http://{url}").hostname
    return host.lower().removeprefix("www.") if host else None


def _root(domain: str) -> str:
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def evaluate_ingest(candidate: Candidate, policy: QualificationPolicy, checker: DomainChecker) -> list[Check]:
    checks: list[Check] = []
    email = (candidate.email or "").strip()
    if not email:
        checks.append(Check("email_present", not policy.require_email, "no contact email", hard=policy.require_email))
        checks.append(Check("website_present", bool(candidate.website) or not policy.require_website,
                            "has a website" if candidate.website else "no website", hard=policy.require_website, weight=0.10))
        return checks
    checks.append(Check("email_present", True, email, hard=False))

    syntax_ok = bool(_EMAIL_RE.match(email)) and ".." not in email and len(email) <= 254
    checks.append(Check("email_syntax", syntax_ok, "valid format" if syntax_ok else "malformed address"))
    if not syntax_ok:
        return checks

    local, domain = email.lower().rsplit("@", 1)
    tld = domain.rsplit(".", 1)[-1]
    placeholder = tld in PLACEHOLDER_TLDS or domain in PLACEHOLDER_DOMAINS
    checks.append(Check("email_not_placeholder", not placeholder, "placeholder/test domain" if placeholder else "real-looking domain"))

    blocked_local = local in BLOCKED_LOCAL_PARTS
    role = local in ROLE_LOCAL_PARTS
    role_ok = policy.allow_role_emails or not role
    checks.append(Check("email_local_part_ok", (not blocked_local) and role_ok,
                        "system/no-reply address" if blocked_local else ("role address not allowed by policy" if not role_ok else "ok")))
    checks.append(Check("personal_not_role_address", not role, "named/personal mailbox" if not role else "role address (info@, contact@...)", hard=False, weight=0.10))

    domain_blocked = (domain in {d.lower() for d in policy.blocked_domains}
                      or tld in {t.lower().lstrip(".") for t in policy.blocked_tlds})
    free = domain in FREE_MAIL_DOMAINS
    checks.append(Check("email_domain_allowed", not domain_blocked and (policy.allow_free_mail or not free),
                        "blocked domain/TLD" if domain_blocked else ("free-mail domain not allowed by policy" if free and not policy.allow_free_mail else "ok")))

    if policy.require_mx and not placeholder:
        status, detail = checker.mx(domain)
        checks.append(Check("email_domain_mx", status == "valid",
                            {"valid": detail, "invalid": f"cannot receive mail: {detail}", "unknown": f"could not verify ({detail})"}[status],
                            retryable=(status == "unknown")))
    checks.append(Check("deliverable_email", all(c.passed for c in checks if c.hard), "all email checks passed", hard=False, weight=0.30))

    checks.append(Check("website_present", bool(candidate.website) or not policy.require_website,
                        "has a website" if candidate.website else "no website", hard=policy.require_website, weight=0.10))
    sd = site_domain(candidate.website)
    match = bool(sd) and _root(sd) == _root(domain)
    checks.append(Check("email_matches_website", match, "email is on the business's own domain" if match else "email domain differs from website", hard=False, weight=0.10))
    title_ok = bool(candidate.contact_title and policy.buyer_titles and any(t.lower() in candidate.contact_title.lower() for t in policy.buyer_titles))
    checks.append(Check("buyer_title_match", title_ok, candidate.contact_title or "no title known", hard=False, weight=0.15))
    return checks


def evaluate_research(research: dict, policy: QualificationPolicy) -> list[Check]:
    status = research.get("status")
    ok = status == "ok"
    checks = [Check("site_reachable", ok, "website read successfully" if ok else f"research status: {status} ({research.get('note', '')})".strip())]
    if not ok:
        return checks
    text = (research.get("text") or "").lower()
    parked = any(m in text for m in PARKED_MARKERS) and len(text) < 1500
    checks.append(Check("site_is_live_business", not parked, "looks like a parked/placeholder page" if parked else "real business content"))
    kws = [k.lower() for k in policy.required_keywords_any]
    if kws:
        hit = [k for k in kws if k in text]
        checks.append(Check("industry_keyword_match", bool(hit), f"matched: {hit}" if hit else f"none of {kws} found on site", weight=0.15))
    else:
        checks.append(Check("industry_keyword_match", True, "no keywords required", hard=False, weight=0.0))
    bad = [k for k in (x.lower() for x in policy.exclude_keywords) if k in text]
    checks.append(Check("no_excluded_keywords", not bad, f"excluded terms present: {bad}" if bad else "ok"))
    hook = research.get("hook")
    checks.append(Check("personalization_hook", bool(hook) or not policy.require_personalization_hook,
                        "a specific true observation is available" if hook else "nothing specific found to personalize on",
                        hard=policy.require_personalization_hook, weight=0.10))
    return checks


@dataclass
class QualificationResult:
    passed: bool
    score: float
    checks: list[Check]
    stage: str

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if c.hard and not c.passed]

    @property
    def only_retryable_failures(self) -> bool:
        """True when the ONLY reason it failed is that something could not be verified right now (a DNS blip): such a
        prospect is neither qualified nor disqualified yet -- it must be re-checked, not discarded."""
        failing = [c for c in self.checks if c.hard and not c.passed]
        return bool(failing) and all(c.retryable for c in failing)

    def to_json(self) -> dict:
        return {
            "passed": self.passed, "score": self.score, "stage": self.stage, "failed": self.failed,
            "pending_retry": self.only_retryable_failures,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail, "hard": c.hard} for c in self.checks],
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }


def score_checks(checks: list[Check]) -> float:
    return round(min(1.0, sum(c.weight for c in checks if c.passed)), 2)


def finalize(stage: str, checks: list[Check], policy: QualificationPolicy, *, require_score: bool) -> QualificationResult:
    hard_ok = all(c.passed for c in checks if c.hard)
    score = score_checks(checks)
    passed = hard_ok and (score >= policy.min_score if require_score else True)
    return QualificationResult(passed=passed, score=score, checks=checks, stage=stage)


def merge_stages(ingest_json: dict, research_checks: list[Check], policy: QualificationPolicy) -> QualificationResult:
    """Final gate: ingest checks (re-hydrated) + research checks, scored together against min_score."""
    ingest_checks = [Check(c["name"], c["passed"], c["detail"], c.get("hard", True), _WEIGHTS.get(c["name"], 0.0)) for c in ingest_json.get("checks", [])]
    return finalize("final", ingest_checks + research_checks, policy, require_score=True)


_WEIGHTS = {"deliverable_email": 0.30, "website_present": 0.10, "email_matches_website": 0.10, "buyer_title_match": 0.15, "personal_not_role_address": 0.10}
