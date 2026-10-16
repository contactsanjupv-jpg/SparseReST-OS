"""Founder control panel for the acquisition autopilot.

  python -m scripts.autopilot_ctl status                  # ON/OFF, mode, capacity, pipeline, readiness
  python -m scripts.autopilot_ctl readiness               # what is missing before real email may be sent
  python -m scripts.autopilot_ctl goal set --customers 5 --days 30   # YOUR goal. Required: autopilot will not start without one.
  python -m scripts.autopilot_ctl goal status | goal extend --days 14
  python -m scripts.autopilot_ctl enable                  # switch ON (founder approves every email); the goal clock starts now
  python -m scripts.autopilot_ctl enable --mode auto --ack-auto
  python -m scripts.autopilot_ctl disable                 # switch OFF (nothing new is planned or sent)
  python -m scripts.autopilot_ctl resume --ack            # clear a safety-breaker pause
  python -m scripts.autopilot_ctl set daily_cap=20 start_hour=9 timezone=America/Chicago
  python -m scripts.autopilot_ctl approvals list | approve --all | approve ID... | reject ID...
  python -m scripts.autopilot_ctl report [--period today|daily|weekly]    # default: today so far
  python -m scripts.autopilot_ctl suppress add email|domain VALUE | suppress list
  python -m scripts.autopilot_ctl leads                   # replied / interested prospects (who to answer, who to book)
  python -m scripts.autopilot_ctl meeting EMAIL           # record that a real meeting was booked with this prospect
  python -m scripts.autopilot_ctl customer EMAIL          # record that this prospect became a paying customer
        (add --external for a deal that did not come through the outreach loop)
  python -m scripts.autopilot_ctl tick                    # run one planning pass now
Add --product SLUG (default: xflow).
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import func

from packages.core import autopilot, outbound, reporting
from packages.core.autopilot_config import ConfigError
from packages.db.models import Approval, GrowthReport, Product, Prospect, Suppression, Task
from packages.db.session import session_scope
from packages.core.services import ApprovalService


def _coerce(value: str):
    try:
        return json.loads(value)
    except ValueError:
        return value


def _resolve(slug: str):
    with session_scope() as session:
        product = session.query(Product).filter_by(slug=slug).one_or_none()
        if product is None:
            raise SystemExit(f"No product '{slug}'")
        return product.company_id, product.id


def main(argv=None, out=print) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--product", default="xflow")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status"); sub.add_parser("readiness"); sub.add_parser("disable"); sub.add_parser("tick")
    e = sub.add_parser("enable"); e.add_argument("--mode", choices=("approval", "auto"), default="approval"); e.add_argument("--ack-auto", action="store_true")
    r = sub.add_parser("resume"); r.add_argument("--ack", action="store_true")
    st = sub.add_parser("set"); st.add_argument("pairs", nargs="+")
    ap = sub.add_parser("approvals"); ap.add_argument("action", choices=("list", "approve", "reject")); ap.add_argument("ids", nargs="*"); ap.add_argument("--all", action="store_true")
    rp = sub.add_parser("report"); rp.add_argument("--period", choices=("today", "daily", "weekly"), default="today"); rp.add_argument("--regenerate", action="store_true")
    sub.add_parser("leads")
    for name in ("meeting", "customer"):
        mc = sub.add_parser(name); mc.add_argument("email"); mc.add_argument("--external", action="store_true")
    gl = sub.add_parser("goal"); gl.add_argument("action", choices=("set", "status", "extend"))
    gl.add_argument("--customers", type=int); gl.add_argument("--days", type=int)
    sp = sub.add_parser("suppress"); sp.add_argument("action", choices=("add", "list")); sp.add_argument("kind", nargs="?", choices=("email", "domain")); sp.add_argument("value", nargs="?")
    args = p.parse_args(argv)
    company_id, product_id = _resolve(args.product)

    try:
        if args.cmd == "status":
            s = autopilot.get_status(company_id, product_id)
            cap = s["capacity"]
            out(f"Autopilot: {s['effective_state']}")
            out("Goal: " + s["goal"]["message"])
            out(f"Today: {cap['used_today']}/{cap['cap']} sent  |  send window open now: {cap['in_window']}" + ("" if cap["in_window"] else f"  (next opens {cap['next_window_open']})"))
            out(f"Pending approvals: {s['pending_approvals']}   Last planning pass: {s['last_tick_at'] or 'never'}")
            out("Pipeline: " + (", ".join(f"{k}={v}" for k, v in sorted(s["pipeline"].items())) or "empty"))
            if s["cursor"].get("exhausted"):
                out("NOTE: prospect discovery is exhausted in every configured location -- broaden the ICP.")
            out("Readiness: " + ("ready" if autopilot.readiness(company_id, product_id)["ready"] else "NOT ready (run: readiness)"))
        elif args.cmd == "readiness":
            rep = autopilot.readiness(company_id, product_id)
            for c in rep["checks"]:
                out(f"[{'OK' if c['ok'] else ('BLOCKING' if c['blocking'] else 'warn')}] {c['name']}: {c['detail']}")
            out("READY" if rep["ready"] else "NOT READY -- fix the BLOCKING items above")
            return 0 if rep["ready"] else 1
        elif args.cmd == "enable":
            s = autopilot.enable(company_id, product_id, mode=args.mode, ack_auto=args.ack_auto)
            out(f"Autopilot is now: {s['effective_state']}")
        elif args.cmd == "disable":
            out(f"Autopilot is now: {autopilot.disable(company_id, product_id)['effective_state']}")
        elif args.cmd == "resume":
            if not args.ack:
                out("Resuming re-enables sending after a safety pause. Confirm you investigated the cause: add --ack")
                return 1
            out("Resumed." if outbound.resume(product_id) else "Sending was not paused.")
        elif args.cmd == "set":
            patch = {}
            for pair in args.pairs:
                key, _, value = pair.partition("=")
                patch[key.strip()] = _coerce(value)
            s = autopilot.update_config(company_id, product_id, patch)
            out("Updated: " + ", ".join(f"{k}={s['config'][k]}" for k in patch))
        elif args.cmd == "tick":
            out(json.dumps(autopilot.tick(company_id, product_id), default=str, indent=2))
        elif args.cmd == "goal":
            from packages.core import campaign
            if args.action == "set":
                if args.customers is None or args.days is None:
                    out("usage: goal set --customers N --days D   (e.g. 5 customers in 30 days)")
                    return 1
                g = campaign.set_goal(product_id, args.customers, args.days)
            elif args.action == "extend":
                if args.days is None:
                    out("usage: goal extend --days N")
                    return 1
                g = campaign.extend(product_id, args.days)
            else:
                g = campaign.progress(product_id)
            out("GOAL: " + g["message"])
        elif args.cmd == "leads":
            _leads(product_id, out)
        elif args.cmd in ("meeting", "customer"):
            return _founder_milestone(args, company_id, product_id, out)
        elif args.cmd == "approvals":
            _approvals(args, product_id, out)
        elif args.cmd == "report":
            _report(args, company_id, product_id, out)
        elif args.cmd == "suppress":
            if args.action == "add":
                if not (args.kind and args.value):
                    out("usage: suppress add email|domain VALUE")
                    return 1
                outbound.suppress(company_id, args.kind, args.value, "manual", "founder")
                out(f"Suppressed {args.kind} {args.value.lower()} -- it will never be contacted.")
            else:
                with session_scope() as session:
                    for row in session.query(Suppression).filter_by(company_id=company_id).order_by(Suppression.created_at.desc()).limit(200).all():
                        out(f"{row.kind:7} {row.value}  ({row.reason}, {row.source}, {row.created_at:%Y-%m-%d})")
    except (autopilot.AutopilotError, ConfigError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _leads(product_id, out) -> None:
    from sqlalchemy import func
    with session_scope() as session:
        rows = session.query(Prospect).filter(Prospect.product_id == product_id, Prospect.reply_classification.in_(
            ("high_intent", "interested", "question", "objection", "maybe"))).order_by(Prospect.replied_at.desc()).all()
        if not rows:
            out("No replies that need you yet.")
        for p in rows:
            out(f"[{p.reply_classification:11}] {p.company_name}  <{p.contact_email}>  stage={p.stage}  replied={p.replied_at:%Y-%m-%d %H:%M}")


def _founder_milestone(args, company_id, product_id, out) -> int:
    """Founder-only, never agent-triggered: a human confirming a real meeting / a real sale."""
    from packages.agents.departments.sales import SalesAgent
    from packages.core.context import Context
    from packages.core.services import TaskService
    from packages.db.models import Agent
    from packages.tools.outreach import LoggingOutreachProvider
    email = args.email.strip().lower()
    with session_scope() as session:
        prospect = session.query(Prospect).filter(Prospect.product_id == product_id, func.lower(Prospect.contact_email) == email).first()
        if prospect is None:
            print(f"Error: no prospect with email {email}", file=sys.stderr)
            return 1
        prospect_id, name = prospect.id, prospect.company_name
        agent_id = session.query(Agent).filter_by(slug="sales").one().id
    action = "mark_meeting_booked" if args.cmd == "meeting" else "mark_customer"
    ctx = Context(company_id=company_id, agent_id=agent_id, product_id=product_id)
    refs = {"action": action, "prospect_id": str(prospect_id), "external": bool(args.external)}
    task_id = TaskService.create(ctx, goal=f"{action} {name}", created_by="founder", context_refs=refs)
    try:
        result = SalesAgent(LoggingOutreachProvider()).execute(ctx, task_id, goal=f"{action} {name}", context_refs=refs)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if result.get("status") == "blocked":
        print(f"Error: {result['reason']}", file=sys.stderr)
        return 1
    out(f"Recorded: {name} -> {'meeting booked' if args.cmd == 'meeting' else 'CUSTOMER'}" + (" (external: not from the outreach loop)." if args.external else "."))
    return 0


def _approvals(args, product_id, out) -> None:
    with session_scope() as session:
        rows = session.query(Approval, Task).join(Task, Task.id == Approval.task_id).filter(
            Approval.status == "pending", Task.product_id == product_id, Approval.requested_by == "sales").order_by(Approval.created_at).all()
        pending = [(a.id, a.summary, a.details or {}) for a, _ in rows]
    if args.action == "list":
        if not pending:
            out("No drafts waiting for approval.")
        for approval_id, summary, d in pending:
            out(f"\n[{approval_id}] {summary}\n  Subject: {d.get('subject')}\n  Source: {d.get('draft_source')}  |  fit score: {d.get('score')}  |  hook: {d.get('hook')}")
            out("  ---\n  " + "\n  ".join(str(d.get("body", "")).splitlines()))
        return
    approved = args.action == "approve"
    targets = [a for a, _, _ in pending] if args.all else [uuid.UUID(i) for i in args.ids]
    if not targets:
        out("Nothing to do (give IDs or --all).")
        return
    done = 0
    for approval_id in targets:
        try:
            ApprovalService.decide(approval_id, approved=approved)
            done += 1
        except ValueError as exc:
            out(f"  {approval_id}: {exc}")
    out(f"{'Approved' if approved else 'Rejected'} {done} draft(s).")


def _report(args, company_id, product_id, out) -> None:
    from datetime import timedelta
    from packages.core.autopilot_config import AutopilotConfig
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        latest = session.query(GrowthReport).filter_by(product_id=product_id, period=args.period).order_by(GrowthReport.period_start.desc()).first() if args.period != "today" else None
        text = latest.summary if latest and not args.regenerate else None
        from packages.db.models import AutopilotState, Product
        state = session.get(AutopilotState, product_id)
        cfg = AutopilotConfig.from_dict(state.config if state else {})
        name = session.get(Product, product_id).name
    if text is None:
        midnight = cfg.day_start_utc(now)
        if args.period == "today":
            start, end = midnight, now                               # today so far, in the campaign's timezone
        else:
            start, end = midnight - timedelta(days=1 if args.period == "daily" else 7), midnight
        m = reporting.compute_metrics(company_id, product_id, start, end, now)
        out(reporting.render_text(name, args.period, m, reporting.find_bottlenecks(m, cfg)))
    else:
        out(text)


if __name__ == "__main__":
    raise SystemExit(main())
