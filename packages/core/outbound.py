"""
OUTBOUND GOVERNOR -- the one place that decides whether an email may leave, and the ledger that proves what did.

Why it is its own layer: limits enforced inside an agent (or an in-memory counter in the SMTP client) can be
bypassed by a restart, a second worker, a retry, or a bug. Here every send must first RESERVE a slot in the
`outbound_messages` table inside a Postgres advisory-locked transaction, so the rules hold across processes
and restarts:

  paused (safety breaker)     -> defer       a tripped breaker blocks ALL sends until the founder resumes
  autopilot OFF               -> defer       (autopilot-originated sends only)
  suppressed email / domain   -> refuse      unsubscribe, hard bounce, not-interested ... never contacted again
  duplicate                   -> refuse      same address already contacted (one follow-up max)
  domain cooldown             -> refuse      one first-contact per company domain per N days (not free-mail)
  daily cap (with warm-up)    -> defer       never exceeded; defers to the next window instead of dropping work
  send window                 -> defer       business hours / days in the recipients' timezone
  pacing                      -> defer       minimum spacing (+jitter) between sends: no bursts

'defer' means the task is rescheduled for `retry_at` without consuming a retry; 'refuse' is terminal.
Agents never touch these tables (admin connection only, REVOKEd from app_role): an agent cannot raise its own
cap, edit the ledger, or un-suppress someone.
"""
from __future__ import annotations

import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, text

from packages.core.autopilot_config import AutopilotConfig
from packages.core.config import get_settings
from packages.core.log import get_logger, log
from packages.core.qualification import FREE_MAIL_DOMAINS
from packages.db.models import AutopilotState, OutboundMessage, Prospect, Suppression
from packages.db.session import session_scope

_log = get_logger("outbound")
LOCK_SEND = 7_301_010
COUNTED = ("reserved", "sent", "unknown")          # statuses that consume daily capacity / block duplicates
STALE_RESERVATION_MINUTES = 30
HARD_SUPPRESS_REASONS = ("unsubscribe", "hard_bounce", "spam_complaint", "not_interested", "wrong_person", "existing_customer", "rejected_by_server")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Decision:
    allowed: bool
    code: str                                  # ok | paused | autopilot_off | suppressed | duplicate | domain_cooldown | daily_cap | outside_window | pacing
    reason: str = ""
    retry_at: Optional[datetime] = None        # set => defer; unset and not allowed => terminal refusal
    message_id: Optional[uuid.UUID] = None

    @property
    def defer(self) -> bool:
        return (not self.allowed) and self.retry_at is not None


def split_email(email: str) -> tuple[str, str]:
    email = email.strip().lower()
    return email, email.rsplit("@", 1)[-1]


# ---------------------------------------------------------------- state / config
def ensure_state(session, product_id: uuid.UUID) -> AutopilotState:
    state = session.get(AutopilotState, product_id)
    if state is None:
        state = AutopilotState(product_id=product_id, enabled=False, mode="approval", config={}, cursor={})
        session.add(state)
        session.flush()
    return state


def load_config(state: Optional[AutopilotState]) -> AutopilotConfig:
    return AutopilotConfig.from_dict(state.config if state else {})


# ---------------------------------------------------------------- suppression
def suppress(company_id: uuid.UUID, kind: str, value: str, reason: str, source: str = "system") -> None:
    value = value.strip().lower()
    if not value:
        return
    with session_scope() as session:
        session.execute(text("""
            INSERT INTO suppressions (id, company_id, kind, value, reason, source)
            VALUES (:id, :c, :k, :v, :r, :s) ON CONFLICT (company_id, kind, value) DO NOTHING
        """), {"id": uuid.uuid4(), "c": company_id, "k": kind, "v": value, "r": reason, "s": source})
    log(_log, logging.INFO, "suppressed", kind=kind, value=value, reason=reason)


def is_suppressed(session, company_id: uuid.UUID, email: str) -> Optional[str]:
    email, domain = split_email(email)
    row = session.query(Suppression).filter(
        Suppression.company_id == company_id,
        ((Suppression.kind == "email") & (Suppression.value == email)) | ((Suppression.kind == "domain") & (Suppression.value == domain)),
    ).first()
    return f"{row.kind} suppressed ({row.reason})" if row else None


# ---------------------------------------------------------------- ledger queries
def count_today(session, company_id: uuid.UUID, cfg: AutopilotConfig, now: datetime) -> int:
    return session.query(func.count(OutboundMessage.id)).filter(
        OutboundMessage.company_id == company_id, OutboundMessage.status.in_(COUNTED),
        OutboundMessage.reserved_at >= cfg.day_start_utc(now),
    ).scalar() or 0


def first_send_at(session, company_id: uuid.UUID) -> Optional[datetime]:
    return session.query(func.min(OutboundMessage.reserved_at)).filter(
        OutboundMessage.company_id == company_id, OutboundMessage.status.in_(("sent", "unknown", "bounced"))).scalar()


def capacity_snapshot(company_id: uuid.UUID, product_id: uuid.UUID, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        cfg = load_config(state)
        used = count_today(session, company_id, cfg, now)
        cap = cfg.effective_cap(first_send_at(session, company_id), now)
    return {"cap": cap, "used_today": used, "remaining": max(0, cap - used), "in_window": cfg.in_window(now),
            "next_window_open": None if cfg.in_window(now) else cfg.next_window_open(now).isoformat()}


# ---------------------------------------------------------------- reserve / confirm
def reserve(*, company_id: uuid.UUID, product_id: uuid.UUID, prospect_id: Optional[uuid.UUID], task_id: Optional[uuid.UUID],
            to_email: str, kind: str, subject: str, approval: str, autopilot: bool,
            now: Optional[datetime] = None, rng: Optional[random.Random] = None) -> Decision:
    now, rng = now or _now(), rng or random.Random()
    email, domain = split_email(to_email)
    with session_scope() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_SEND})      # serialises every reservation, across processes
        state = session.get(AutopilotState, product_id)
        cfg = load_config(state)

        if state and state.paused_reason:
            return Decision(False, "paused", f"sending paused by safety breaker: {state.paused_reason}", retry_at=now + timedelta(minutes=30))
        if autopilot and not (state and state.enabled):
            return Decision(False, "autopilot_off", "autopilot is OFF -- held until it is switched on", retry_at=now + timedelta(minutes=30))
        if autopilot and state.campaign_status in ("completed", "expired"):
            # Backstop: the planner cancels unsent work when a campaign ends; if anything slips through, it still cannot send.
            return Decision(False, "campaign_ended", f"campaign {state.campaign_status}: no new outreach", retry_at=now + timedelta(hours=6))

        why = is_suppressed(session, company_id, email)
        if why:
            return Decision(False, "suppressed", why)

        dup_filter = [OutboundMessage.company_id == company_id, OutboundMessage.to_email == email,
                      OutboundMessage.kind == kind, OutboundMessage.status.in_(COUNTED)]
        if kind == "first_contact" and session.query(OutboundMessage.id).filter(*dup_filter).first():
            return Decision(False, "duplicate", f"{email} was already contacted")
        if kind == "follow_up" and session.query(OutboundMessage.id).filter(*dup_filter).first():
            return Decision(False, "duplicate", f"{email} already received its one follow-up")

        if kind == "first_contact" and domain not in FREE_MAIL_DOMAINS and cfg.domain_cooldown_days:
            recent = session.query(OutboundMessage).filter(
                OutboundMessage.company_id == company_id, OutboundMessage.to_domain == domain,
                OutboundMessage.status.in_(COUNTED), OutboundMessage.reserved_at >= now - timedelta(days=cfg.domain_cooldown_days),
                OutboundMessage.prospect_id != prospect_id).first()
            if recent:
                return Decision(False, "domain_cooldown", f"another contact at {domain} was emailed within {cfg.domain_cooldown_days} days")

        if autopilot:
            if not cfg.in_window(now):
                return Decision(False, "outside_window", "outside the configured send window", retry_at=cfg.next_window_open(now))

        cap = cfg.effective_cap(first_send_at(session, company_id), now)
        if count_today(session, company_id, cfg, now) >= cap:
            return Decision(False, "daily_cap", f"daily cap of {cap} reached", retry_at=cfg.next_window_open(now + timedelta(hours=max(1, 24 - cfg.local(now).hour))))

        if autopilot:
            last = session.query(func.max(OutboundMessage.reserved_at)).filter(
                OutboundMessage.company_id == company_id, OutboundMessage.status.in_(COUNTED)).scalar()
            if last is not None:
                gap = cfg.gap_seconds(cap)
                earliest = last + timedelta(seconds=gap * (1 + rng.uniform(-cfg.jitter, cfg.jitter)))
                if now < earliest:
                    slot = earliest + timedelta(seconds=rng.uniform(0, gap * 0.3))
                    if not cfg.in_window(slot):
                        slot = cfg.next_window_open(slot)
                    return Decision(False, "pacing", "spacing sends out (no bursts)", retry_at=slot)

        message = OutboundMessage(
            id=uuid.uuid4(), company_id=company_id, product_id=product_id, prospect_id=prospect_id, task_id=task_id,
            kind=kind, to_email=email, to_domain=domain, subject=subject[:200], status="reserved", approval=approval, reserved_at=now)
        session.add(message)
        session.flush()
        return Decision(True, "ok", message_id=message.id)


def real_first_contacts(company_id: uuid.UUID, emails: list[str]) -> set[str]:
    """Which of these addresses were REALLY emailed (a first contact the provider accepted). `logged` (dry run) and
    `failed` rows do not count -- the follow-up path is only open to people who actually received a first email."""
    wanted = [e.strip().lower() for e in emails if e]
    if not wanted:
        return set()
    with session_scope() as session:
        rows = session.query(OutboundMessage.to_email).filter(
            OutboundMessage.company_id == company_id, OutboundMessage.kind == "first_contact",
            OutboundMessage.status.in_(("sent", "unknown", "bounced")), OutboundMessage.to_email.in_(wanted)).distinct().all()
    return {r[0] for r in rows}


def _update(message_id: uuid.UUID, **values) -> None:
    with session_scope() as session:
        message = session.get(OutboundMessage, message_id)
        if message is not None:
            for k, v in values.items():
                setattr(message, k, v)


def confirm_sent(message_id: uuid.UUID, provider_message_id: Optional[str], now: Optional[datetime] = None) -> None:
    _update(message_id, status="sent", provider_message_id=provider_message_id, sent_at=now or _now())


def confirm_logged(message_id: uuid.UUID, note: str) -> None:
    """The provider did not actually send (stub/logging mode): the reservation must not consume real capacity."""
    _update(message_id, status="logged", error=note[:500])


def mark_failed(message_id: uuid.UUID, error: str, kind: str) -> None:
    """kind: 'recipient' (this address was refused), 'systemic' (auth/connection -- our side), 'transient'."""
    _update(message_id, status="failed", error=f"{kind}: {error}"[:500])


# ---------------------------------------------------------------- bounces
_FINAL_RECIPIENT = re.compile(r"(?:final|original)-recipient:\s*rfc822;\s*<?([^\s<>;]+@[^\s<>;]+)>?", re.I)
_STATUS_CODE = re.compile(r"\bstatus:\s*([245])\.(\d{1,3})\.(\d{1,3})", re.I)
_SMTP_CODE = re.compile(r"\b(5\d\d)[ -]\s*(?:5\.\d\.\d\s)?", re.I)
_ANGLE_ADDR = re.compile(r"<([^\s<>@]+@[^\s<>]+)>")
_BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem", "mailer_daemon")
_BOUNCE_SUBJECTS = ("undelivered mail", "delivery status notification", "mail delivery failed", "returned mail", "failure notice",
                    "delivery failure", "undeliverable", "message not delivered", "could not be delivered")
_SOFT_HINTS = ("mailbox full", "over quota", "temporarily", "try again later", "greylist", "4.2.2", "4.4.", "4.7.")


@dataclass
class Bounce:
    recipient: Optional[str]
    hard: bool
    detail: str


def parse_bounce(from_email: str, subject: str, body: str) -> Optional[Bounce]:
    """Recognises a delivery-failure notice (DSN) and extracts who it failed for and whether it is permanent."""
    sender, subj, lowered = (from_email or "").lower(), (subject or "").lower(), (body or "").lower()
    if not (any(s in sender for s in _BOUNCE_SENDERS) or any(s in subj for s in _BOUNCE_SUBJECTS)):
        return None
    recipient = None
    m = _FINAL_RECIPIENT.search(body or "")
    if m:
        recipient = m.group(1).lower()
    else:
        candidates = [a.lower() for a in _ANGLE_ADDR.findall(body or "") if not any(x in a.lower() for x in _BOUNCE_SENDERS)]
        recipient = candidates[0] if candidates else None
    status = _STATUS_CODE.search(body or "")
    hard = bool(status and status.group(1) == "5") or bool(_SMTP_CODE.search(body or "")) or "user unknown" in lowered or "does not exist" in lowered or "no such user" in lowered
    if status and status.group(1) == "4" or any(h in lowered for h in _SOFT_HINTS) and not (status and status.group(1) == "5"):
        hard = False
    detail = (status.group(0) if status else "") or (subject or "")[:120]
    return Bounce(recipient, hard, detail)


def record_bounce(company_id: uuid.UUID, recipient: str, hard: bool, detail: str, now: Optional[datetime] = None) -> Optional[uuid.UUID]:
    """Marks the matching ledger row bounced; a HARD bounce also suppresses the address and flags the prospect.
    Returns the product_id affected (so the caller can re-evaluate the breaker)."""
    email, _ = split_email(recipient)
    now = now or _now()
    product_id = None
    with session_scope() as session:
        message = session.query(OutboundMessage).filter(
            OutboundMessage.company_id == company_id, OutboundMessage.to_email == email, OutboundMessage.status.in_(("sent", "unknown", "bounced"))
        ).order_by(OutboundMessage.reserved_at.desc()).first()
        if message is None:
            return None
        message.status, message.bounce_type, message.bounced_at, message.error = "bounced", "hard" if hard else "soft", now, detail[:300]
        product_id = message.product_id
        if hard and message.prospect_id:
            prospect = session.get(Prospect, message.prospect_id)
            if prospect is not None:
                prospect.do_not_contact, prospect.do_not_contact_reason = True, "hard bounce"
    if hard:
        suppress(company_id, "email", email, "hard_bounce", "bounce_notice")
    return product_id


# ---------------------------------------------------------------- circuit breaker
def trip_breaker(company_id: uuid.UUID, product_id: uuid.UUID, reason: str, now: Optional[datetime] = None) -> bool:
    """Pauses ALL sending for the product and raises ONE critical incident. Returns True if newly tripped."""
    from packages.core.incidents import IncidentService
    with session_scope() as session:
        state = ensure_state(session, product_id)
        if state.paused_reason:
            return False
        state.paused_reason, state.paused_at = reason, now or _now()
    IncidentService().report(
        company_id=company_id, product_id=product_id, agent_id=None, task_id=None, severity="critical",
        title="Outbound email PAUSED by a safety breaker", cause=reason,
        impact="No emails will be sent until you resume. Nothing is lost: queued sends wait, replies and reports keep running.",
        action_required="Investigate the cause (bounces/complaints/SMTP errors), fix it, then resume with: python -m scripts.autopilot_ctl resume --ack",
        dedup_key="safety_pause")
    log(_log, logging.CRITICAL, "safety breaker tripped", reason=reason, product_id=str(product_id))
    return True


def resume(product_id: uuid.UUID, by: str = "founder") -> bool:
    from packages.db.models import Incident
    with session_scope() as session:
        state = session.get(AutopilotState, product_id)
        if state is None or not state.paused_reason:
            return False
        state.paused_reason, state.paused_at, state.updated_by = None, None, by
        for incident in session.query(Incident).filter_by(product_id=product_id, dedup_key="safety_pause", status="open").all():
            incident.status, incident.resolved_at = "resolved", _now()
    return True


def evaluate_health(company_id: uuid.UUID, product_id: uuid.UUID, now: Optional[datetime] = None) -> Optional[str]:
    """Checks bounce rate, systemic failures and complaint/unsubscribe rate; trips the breaker if any is out of bounds."""
    with session_scope() as session:
        cfg = load_config(session.get(AutopilotState, product_id))
        recent = session.query(OutboundMessage).filter(
            OutboundMessage.company_id == company_id, OutboundMessage.status.in_(("sent", "bounced", "failed", "unknown")),
        ).order_by(OutboundMessage.reserved_at.desc()).limit(max(cfg.bounce_window, cfg.consecutive_failure_pause)).all()

        delivered_like = [m for m in recent if m.status in ("sent", "bounced", "unknown")][:cfg.bounce_window]
        if len(delivered_like) >= cfg.bounce_min_sample:
            hard = sum(1 for m in delivered_like if m.status == "bounced" and m.bounce_type == "hard")
            rate = hard / len(delivered_like)
            if rate >= cfg.bounce_pause_rate:
                reason = f"hard-bounce rate {rate:.0%} over the last {len(delivered_like)} sends (limit {cfg.bounce_pause_rate:.0%}) -- bad addresses damage the sending domain's reputation"
                trip_breaker(company_id, product_id, reason, now)
                return reason

        last = recent[:cfg.consecutive_failure_pause]
        if len(last) >= cfg.consecutive_failure_pause and all(m.status == "failed" and not (m.error or "").startswith("recipient") for m in last):
            reason = f"{cfg.consecutive_failure_pause} consecutive sends failed on our side ({last[0].error})"
            trip_breaker(company_id, product_id, reason, now)
            return reason

        contacted = session.query(OutboundMessage.prospect_id).filter(
            OutboundMessage.company_id == company_id, OutboundMessage.kind == "first_contact", OutboundMessage.status.in_(("sent", "bounced", "unknown")),
        ).order_by(OutboundMessage.reserved_at.desc()).limit(30).all()
        ids = [c[0] for c in contacted if c[0]]
        if len(ids) >= cfg.complaint_min_sample:
            bad = session.query(func.count(Prospect.id)).filter(Prospect.id.in_(ids), Prospect.reply_classification.in_(("unsubscribe", "spam"))).scalar() or 0
            rate = bad / len(ids)
            if rate >= cfg.complaint_pause_rate:
                reason = f"{rate:.0%} of the last {len(ids)} contacted prospects unsubscribed or reported spam (limit {cfg.complaint_pause_rate:.0%}) -- the message or targeting is hurting reputation"
                trip_breaker(company_id, product_id, reason, now)
                return reason
    return None


def mark_stale_reservations(now: Optional[datetime] = None) -> int:
    """A reservation with no outcome (worker died between reserve and send) may or may not have been delivered.
    It keeps counting toward the cap (conservative) but is relabelled so it is visible."""
    cutoff = (now or _now()) - timedelta(minutes=STALE_RESERVATION_MINUTES)
    with session_scope() as session:
        return session.query(OutboundMessage).filter(OutboundMessage.status == "reserved", OutboundMessage.reserved_at < cutoff).update({"status": "unknown", "error": "no outcome recorded (worker interrupted)"})
