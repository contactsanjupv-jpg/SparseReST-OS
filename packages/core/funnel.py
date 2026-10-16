"""
Funnel-bottleneck baseline: is a conversion rate bad, or merely what it always is?

The Growth agent's diagnose_funnel answers "which transition is weakest right now?".
That is necessary but not sufficient -- outreach->reply is ALWAYS the weakest step in
cold outreach, so flagging it forever is noise. This module answers the better question:
"which transition has gotten worse compared with its own trailing baseline?"

Method (cohort-based, so late conversions don't masquerade as drops):
  * For each tracked transition A->B, take the COHORT of prospects that entered A during
    a window, and measure what fraction of that cohort has reached B since.
  * CURRENT cohort  = entered A in (now - maturity - window, now - maturity]
    BASELINE cohort = entered A in the `baseline` days before the current window.
    `maturity` days are skipped so prospects who simply haven't had time to reply yet
    are not counted as failures.
  * A transition is `degraded` when its current rate falls below `degradation_ratio` x its
    baseline rate, with at least MIN_COHORT prospects in BOTH cohorts. Smaller samples are
    reported as `insufficient_data` -- never as a finding.
  * Separately, the `bottleneck` is the transition losing the most prospects in absolute
    terms relative to a sane floor, so the founder sees where volume is being lost even
    before there is a baseline to compare against.

Nothing here invents data: no events, no findings.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from packages.db.models import FunnelEvent

MIN_COHORT = 5
DEFAULT_WINDOW_DAYS = 14
DEFAULT_BASELINE_DAYS = 28
DEFAULT_MATURITY_DAYS = 3
DEFAULT_DEGRADATION_RATIO = 0.6

# The transitions this system actually emits events for (see Prospecting/Sales/CS agents).
TRACKED_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("qualified", "outreach_sent"),
    ("outreach_sent", "reply_classified"),
    ("reply_classified", "meeting_booked"),
    ("meeting_booked", "customer"),
    ("customer", "onboarded"),
)


@dataclass(frozen=True)
class Event:
    prospect_id: uuid.UUID
    to_stage: str
    at: datetime


@dataclass
class TransitionReport:
    from_stage: str
    to_stage: str
    current_cohort: int
    current_converted: int
    current_rate: Optional[float]
    baseline_cohort: int
    baseline_converted: int
    baseline_rate: Optional[float]
    status: str                      # healthy | degraded | improved | insufficient_data | no_baseline
    relative_change: Optional[float]  # (current - baseline) / baseline


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _first_reach(events: Iterable[Event]) -> dict[str, dict[uuid.UUID, datetime]]:
    first: dict[str, dict[uuid.UUID, datetime]] = defaultdict(dict)
    for e in events:
        at = _aware(e.at)
        prior = first[e.to_stage].get(e.prospect_id)
        if prior is None or at < prior:
            first[e.to_stage][e.prospect_id] = at
    return first


def _cohort_rate(first, a: str, b: str, start: datetime, end: datetime) -> tuple[int, int]:
    cohort = {pid: at for pid, at in first.get(a, {}).items() if start < at <= end}
    converted = sum(1 for pid, at in cohort.items() if pid in first.get(b, {}) and first[b][pid] >= at)
    return len(cohort), converted


def compute_funnel_report(events: Iterable[Event], *, now: Optional[datetime] = None,
                          window_days: int = DEFAULT_WINDOW_DAYS, baseline_days: int = DEFAULT_BASELINE_DAYS,
                          maturity_days: int = DEFAULT_MATURITY_DAYS,
                          degradation_ratio: float = DEFAULT_DEGRADATION_RATIO) -> dict:
    now = _aware(now or datetime.now(timezone.utc))
    first = _first_reach(events)

    cur_end = now - timedelta(days=maturity_days)
    cur_start = cur_end - timedelta(days=window_days)
    base_end = cur_start
    base_start = base_end - timedelta(days=baseline_days)

    transitions: list[TransitionReport] = []
    for a, b in TRACKED_TRANSITIONS:
        c_n, c_ok = _cohort_rate(first, a, b, cur_start, cur_end)
        b_n, b_ok = _cohort_rate(first, a, b, base_start, base_end)
        c_rate = round(c_ok / c_n, 4) if c_n else None
        b_rate = round(b_ok / b_n, 4) if b_n else None

        if c_n < MIN_COHORT:
            status, change = "insufficient_data", None
        elif b_n < MIN_COHORT or b_rate is None:
            status, change = "no_baseline", None
        else:
            change = round((c_rate - b_rate) / b_rate, 4) if b_rate > 0 else (0.0 if c_rate == 0 else 1.0)
            if b_rate > 0 and c_rate < b_rate * degradation_ratio:
                status = "degraded"
            elif b_rate > 0 and c_rate > b_rate * (1 / degradation_ratio):
                status = "improved"
            else:
                status = "healthy"
        transitions.append(TransitionReport(a, b, c_n, c_ok, c_rate, b_n, b_ok, b_rate, status, change))

    measurable = [t for t in transitions if t.current_rate is not None and t.current_cohort >= MIN_COHORT]
    lost = lambda t: t.current_cohort - t.current_converted                 # noqa: E731 -- prospects that did NOT advance
    largest_leak = max(measurable, key=lost) if measurable else None
    degraded = [t for t in transitions if t.status == "degraded"]

    return {
        "as_of": now.isoformat(),
        "windows": {
            "current": [cur_start.isoformat(), cur_end.isoformat()],
            "baseline": [base_start.isoformat(), base_end.isoformat()],
            "maturity_days": maturity_days,
        },
        "transitions": [asdict(t) for t in transitions],
        "degraded": [f"{t.from_stage}->{t.to_stage}" for t in degraded],
        "largest_leak": (
            {"transition": f"{largest_leak.from_stage}->{largest_leak.to_stage}", "prospects_not_advanced": lost(largest_leak),
             "current_rate": largest_leak.current_rate}
            if largest_leak else None
        ),
        "verdict": (
            "degraded" if degraded
            else "insufficient_data" if not measurable
            else "healthy"
        ),
        "note": (
            "Diagnose a degraded step before sending more outreach -- more volume into a leaking step just "
            "wastes sending reputation."
            if degraded else
            "No measured transition has fallen meaningfully below its own trailing baseline."
            if measurable else
            f"Fewer than {MIN_COHORT} prospects in every cohort -- too little data to call anything a finding."
        ),
    }


def load_events(session, product_id: uuid.UUID) -> list[Event]:
    rows = session.query(FunnelEvent.prospect_id, FunnelEvent.to_stage, FunnelEvent.created_at).filter_by(product_id=product_id).all()
    return [Event(r[0], r[1], r[2]) for r in rows]


def funnel_report_for_product(product_id: uuid.UUID, **kwargs) -> dict:
    from packages.db.session import session_scope
    with session_scope() as session:
        events = load_events(session, product_id)
    return compute_funnel_report(events, **kwargs)


def report_degradation_incidents(company_id: uuid.UUID, product_id: uuid.UUID, report: dict, *, growth_agent_id: uuid.UUID) -> list[uuid.UUID]:
    """Turns a `degraded` verdict into ONE open warning Incident per degraded transition.
    Idempotent: if an open incident for that exact transition already exists, it is not
    duplicated every maintenance cycle."""
    from packages.core.incidents import IncidentService
    from packages.db.models import Incident
    from packages.db.session import session_scope

    created: list[uuid.UUID] = []
    for label in report["degraded"]:
        title = f"Funnel degradation: {label}"
        with session_scope() as session:
            exists = session.query(Incident.id).filter_by(product_id=product_id, title=title, status="open").first()
        if exists:
            continue
        t = next(x for x in report["transitions"] if f"{x['from_stage']}->{x['to_stage']}" == label)
        created.append(IncidentService().report(
            company_id=company_id, product_id=product_id, agent_id=growth_agent_id, task_id=None, severity="warning",
            title=title,
            cause=f"Conversion fell to {t['current_rate']:.0%} (n={t['current_cohort']}) from a trailing baseline of {t['baseline_rate']:.0%} (n={t['baseline_cohort']}).",
            impact="Prospects are leaking at this step faster than usual; more outreach volume would amplify the leak, not fix it.",
            action_required="Inspect recent messaging, targeting, and deliverability for this step before increasing volume.",
        ))
    return created
