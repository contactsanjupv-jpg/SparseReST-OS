"""
Turns a failure into a structured Incident (what broke, where, why, impact,
what the system did, what you need to do) -- never just a log line saying
"something went wrong." See docs/ARCHITECTURE.md's failure/alert requirements.

NotificationAdapter is pluggable the same way EnrichmentTool/OutreachProvider
are: ConsoleNotifier is the $0-cost default (prints/logs), a real Slack/email
notifier is a later config-driven swap -- IncidentService's own code never
changes when that happens.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Optional

from packages.db.models import SEVERITIES, Incident
from packages.db.session import session_scope


class NotificationAdapter(ABC):
    @abstractmethod
    def notify(self, incident: "IncidentPayload") -> None:
        raise NotImplementedError


class IncidentPayload:
    def __init__(self, id: uuid.UUID, severity: str, title: str, cause: Optional[str],
                 impact: Optional[str], action_required: Optional[str]):
        self.id, self.severity, self.title = id, severity, title
        self.cause, self.impact, self.action_required = cause, impact, action_required

    def format(self) -> str:
        icon = {"info": "\u2139\ufe0f", "warning": "\U0001f7e1", "error": "\U0001f7e0", "critical": "\U0001f534"}.get(self.severity, "")
        lines = [f"{icon} {self.severity.upper()} -- {self.title}"]
        if self.cause:
            lines.append(f"Cause: {self.cause}")
        if self.impact:
            lines.append(f"Impact: {self.impact}")
        if self.action_required:
            lines.append(f"Action required: {self.action_required}")
        return "\n".join(lines)


class ConsoleNotifier(NotificationAdapter):
    """$0-cost default: writes the formatted incident to stdout/log. A real
    Slack/email/SMS notifier implements the same `notify()` method -- nothing
    else in the incident pipeline changes when one is wired in."""

    def notify(self, incident: IncidentPayload) -> None:
        print(incident.format())  # noqa: T201 -- intentional: this IS the notification for the $0-cost default


class SlackWebhookNotifier(NotificationAdapter):
    """Real notification via a Slack incoming webhook -- only active when
    SLACK_WEBHOOK_URL is configured (see get_notifier() below). A failed
    webhook POST is logged, never raised -- a broken Slack integration must
    not be the reason an agent's failure handling itself fails."""

    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url

    def notify(self, incident: IncidentPayload) -> None:
        import httpx
        try:
            httpx.post(self._webhook_url, json={"text": incident.format()}, timeout=5.0)
        except Exception as exc:
            print(f"SlackWebhookNotifier failed to deliver incident {incident.id}: {exc}")  # noqa: T201
            print(incident.format())  # noqa: T201 -- fall back to console so the alert is never fully lost


def get_notifier() -> NotificationAdapter:
    from packages.core.config import get_settings
    settings = get_settings()
    if settings.has_live_notifications:
        return SlackWebhookNotifier(settings.slack_webhook_url)
    return ConsoleNotifier()


NOTIFY_SEVERITIES = ("warning", "error", "critical")  # info is recorded but doesn't page anyone


class IncidentService:
    def __init__(self, notifier: Optional[NotificationAdapter] = None):
        self._notifier = notifier or get_notifier()

    def report(self, *, company_id, product_id, agent_id, task_id, severity: str,
               title: str, cause: Optional[str] = None, impact: Optional[str] = None,
               action_required: Optional[str] = None, dedup_key: Optional[str] = None) -> uuid.UUID:
        """dedup_key: at most ONE open incident per (product, dedup_key). Reporting the same condition again
        while it is still open returns the existing incident and does not notify again -- this is what keeps a
        persistent problem (paused autopilot, missing config, backlog) from paging the founder every cycle."""
        if severity not in SEVERITIES:
            raise ValueError(f"Unknown severity '{severity}', expected one of {SEVERITIES}")

        with session_scope() as session:
            if dedup_key is not None:
                existing = session.query(Incident).filter_by(product_id=product_id, dedup_key=dedup_key, status="open").first()
                if existing is not None:
                    return existing.id
            incident = Incident(
                id=uuid.uuid4(), company_id=company_id, product_id=product_id, agent_id=agent_id,
                task_id=task_id, severity=severity, title=title, cause=cause, impact=impact,
                action_required=action_required, status="open", dedup_key=dedup_key,
            )
            session.add(incident)
            session.flush()
            incident_id = incident.id

        if severity in NOTIFY_SEVERITIES:
            self._notifier.notify(IncidentPayload(incident_id, severity, title, cause, impact, action_required))

        return incident_id

    def escalate(self, incident_id: uuid.UUID, note: str) -> None:
        """Re-notify (once) about an incident that has gone unanswered, at critical severity."""
        from datetime import datetime, timezone
        with session_scope() as session:
            incident = session.get(Incident, incident_id)
            if incident is None or incident.status != "open" or incident.escalated_at is not None:
                return
            incident.escalated_at = datetime.now(timezone.utc)
            incident.severity = "critical"
            payload = IncidentPayload(incident.id, "critical", f"STILL UNANSWERED -- {incident.title}", incident.cause, incident.impact,
                                      f"{note} {incident.action_required or ''}".strip())
        self._notifier.notify(payload)

    @staticmethod
    def resolve(incident_id: uuid.UUID) -> None:
        from datetime import datetime, timezone
        with session_scope() as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                raise ValueError(f"Incident {incident_id} not found")
            incident.status = "resolved"
            incident.resolved_at = datetime.now(timezone.utc)
