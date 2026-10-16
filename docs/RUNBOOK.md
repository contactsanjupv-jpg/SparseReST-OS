# XFlow Acquisition -- Production Runbook

One document: what you must provide, how to go live, what to do each day, what to do when something breaks.
(Server install details are in `DEPLOYMENT.md`; every environment variable is in `ENVIRONMENT.md`.)

## 0. What this machine does -- and does not do

**Does, automatically, around the clock:** finds local businesses (free OpenStreetMap data) -> checks their email domain
(DNS) -> reads their own website -> decides if they qualify (hard gate) -> writes a short personalised email -> waits for
your approval -> sends it paced across business hours (max 30/day) -> reads replies from your mailbox -> classifies them
-> sends ONE follow-up to people who stayed silent -> alerts you about hot leads -> emails you a growth report daily and weekly.
It retries failures, refuses to contact anyone who opted out or bounced, and pauses itself if deliverability looks bad.

**Does not:** reply to prospects, take meetings, close sales, or guarantee customers. Those stay with you by design.
It cannot make cold email work if the offer or the target market is wrong -- the weekly report shows you where it leaks.

**Honest status:** every piece is tested (374 automated tests, including a full discover-to-follow-up simulation), but the
live connections -- OpenStreetMap, your SMTP/IMAP mailbox, DNS, Claude -- have never been run from the build environment,
only mocked. Your first day of dry-run + approval mode (section 4) exists precisely to prove them before anything is at stake.

---

## 1. The decisions only you can make

| Decision | Where it goes | Notes |
|---|---|---|
| **Who XFlow is for** (industry + city/region) | `icp.industries` / `icp.osm_tags`, `icp.geography(ies)` | Free discovery covers local trades/shops well (hvac, plumbing, roofing, landscaping, pest_control, electrician, painting, or any OpenStreetMap tag). It is thin for businesses OSM doesn't map -- spot-check a sample before committing. |
| **What XFlow does, in one honest sentence** | `positioning.one_liner` | This is the core of every email. Only claims you can substantiate. |
| **What makes a prospect "qualified"** | `icp.qualification` (keywords on their site, min score...) | The hard gate. Stricter = fewer emails, better ones. It is never loosened automatically. |
| **Your goal: how many customers, by when** | `goal.customers`, `goal.days` (or `autopilot_ctl goal set --customers N --days D`) | **Required -- autopilot will not start without it.** The clock starts when you enable autopilot, and only customers recorded after that count. Reaching the target or the deadline **stops new outreach** (replies and reports continue). |
| **Your sending identity** | mailbox + `sales.sender_name` | Strongly recommended: a **separate domain/mailbox for outreach** (e.g. `xflow-hello.com`), not your main company domain, so a deliverability problem cannot touch your real email. |
| **Postal address** | `sales.physical_address` | Legally required in every commercial email (CAN-SPAM). The system refuses to send without it. |

Fill in `docs/xflow_config.example.yaml` (copy it to `xflow_config.yaml`). The tool refuses a file that still contains `REPLACE_ME`.

## 2. Exactly what to provide

**Required to go live**
1. **The completed `xflow_config.yaml`** (section 1), including your goal.
2. **Outreach mailbox, SMTP:** host, port (587), username, app-password, from-address. Stored with `secrets_cli`, never in `.env`.
3. **The same mailbox, IMAP:** host (+ username/password if different). This is how replies, unsubscribes and bounces are read.
   Live sending is *blocked* without it -- an unsubscribe you can't read is a legal problem.
4. **DNS for the sending domain:** an **SPF** record (autopilot refuses to start without it) and **DKIM + DMARC** (strongly recommended; DKIM is set up in your mail provider's admin console).
5. **Your founder login:** the email to create your account with (`scripts/create_founder.py`).
6. **A server + two hostnames** (e.g. `api.yourdomain.com`, `app.yourdomain.com`) -> `ALLOWED_ORIGINS`, `TRUSTED_HOSTS`, `PUBLIC_API_URL`.
7. **`REPORT_EMAIL_TO`:** where the daily/weekly report is sent (your own inbox).

**Optional but recommended**
8. `ANTHROPIC_API_KEY` -- real reasoning for copywriting and reply classification (otherwise: deterministic templates + keyword rules; still safe, just plainer). The only paid item, and optional.
9. `SLACK_WEBHOOK_URL` -- hot-lead and safety alerts to your phone (free Slack). Without it, alerts reach the console and the report email only.

Nothing else is needed: discovery, research, DNS checks, SMTP/IMAP, scheduling, secrets and reporting are all free/self-hosted.

## 3. One-time setup (about an hour)

```bash
# 3.1  Server up (see DEPLOYMENT.md), then create your login and store credentials
python -m scripts.create_founder you@yourdomain.com
python -m scripts.secrets_cli set smtp_password          # prompts; value never printed
python -m scripts.secrets_cli set imap_password
# .env:  SMTP_PASSWORD=secret://smtp_password   IMAP_PASSWORD=secret://imap_password
#        OUTREACH_MODE=smtp  ENRICHMENT_MODE=free  SMTP_HOST=...  SMTP_FROM_EMAIL=...  IMAP_HOST=...  REPORT_EMAIL_TO=...

# 3.2  Load XFlow's configuration (validate first)
python -m scripts.configure_product xflow xflow_config.yaml --dry-run
python -m scripts.configure_product xflow xflow_config.yaml

python -m scripts.autopilot_ctl goal set --customers 5 --days 30     # (or put `goal:` in the YAML) -- YOUR numbers

# 3.3  Prove the setup: this lists every problem in plain English
python -m scripts.check_production
python -m scripts.autopilot_ctl readiness
```
Docker: prefix with `docker compose run --rm api` (mount the YAML: `-v $PWD/xflow_config.yaml:/app/xflow_config.yaml`).
Start the long-running processes: `docker compose up -d --scale worker=2` (the worker also runs the autopilot planner; `inbound` reads replies).

## 4. Go-live ramp (do not skip)

| Day | Mode | What you do |
|---|---|---|
| **0 -- dry run** | `OUTREACH_MODE=stub`, `ENRICHMENT_MODE=free` | `autopilot_ctl enable` (this also starts your goal clock -- if you want it to start at go-live, run `goal set` again then). Real prospects are found, researched, qualified, drafted -- **nothing is emailed**, and a rehearsed prospect stays untouched: it is eligible for a real first email the moment you switch to live (it never receives a follow-up for an email that was not sent). Read 10 drafts: `autopilot_ctl approvals list`. Are they accurate, honest, in your voice? Fix `positioning` until yes. |
| **1 -- first real sends** | `OUTREACH_MODE=smtp`, approval mode | Send yourself a test first (put your own address through as a prospect or send from the mailbox manually). Then `enable`. Warm-up caps you at **10/day**. Approve the day's drafts in one go. |
| **2-7** | approval mode | Daily routine (section 5). Watch the report's bounce rate (target < 3%) and any `[WARN]`/`[CRITICAL]` items. |
| **8-14** | approval mode | Cap rises to 20/day automatically. |
| **15+** | approval mode | Cap reaches 30/day. **Stay in approval mode** until you trust it. |
| **Optional later** | `enable --mode auto --ack-auto` | Unlocks only after you have approved 10 drafts yourself with < 20% rejected. Removes per-email approval; **every limit, gate and breaker still applies.** Switch back anytime: `autopilot_ctl enable --mode approval`. |

## 5. Your daily routine (about 10 minutes)

1. **Read the report** (email/Slack; or `autopilot_ctl report` for today so far). It leads with `GOAL: n / <your target> customers in <your days>`, then bottlenecks, then **NEEDS YOU**.
2. **Approve the day's drafts:** `autopilot_ctl approvals list` -> `autopilot_ctl approvals approve --all` (or the Approvals tab in the dashboard, which shows each email). Reject any you dislike -- rejecting removes that prospect permanently.
3. **Answer hot leads yourself** (`autopilot_ctl leads`). When a meeting is set: `autopilot_ctl meeting their@email.com`. When they pay: `autopilot_ctl customer their@email.com`. These two commands are how your goal is counted -- the agents can never record them. They are validated: the prospect must really have been emailed by this system and be at the right stage. A deal that came from somewhere else: add `--external` (it counts toward the goal, and is reported separately). `autopilot_ctl goal status` shows progress and pace; `goal extend --days N` adds time.
4. Weekly: read the weekly report's **BOTTLENECKS** and change one thing (targeting, copy, or the gate).

Everything else -- retries, follow-ups, bounces, unsubscribes, dedup, pacing -- needs nothing from you.

## 6. What is automatic vs. what pages you

| Event | The system | You |
|---|---|---|
| Prospect fails the gate | Records exactly which checks failed; never contacts them | Nothing |
| Email send fails (network, 4xx) | Retries with backoff | Nothing |
| Address rejected / hard bounce | Suppresses it forever, counts toward the bounce breaker | Nothing |
| Someone unsubscribes / says "not interested" | Stops all contact, adds to suppression list | Nothing |
| "Question / objection / maybe" reply | Logs an info incident, includes it in the report | Reply when convenient |
| **High-intent / interested reply** | Warning alert with their exact words + email; re-alerts at **critical** after 4h unanswered | **Reply yourself** |
| Drafts waiting > 12h | One reminder (clears itself) | Approve |
| Drafts waiting > 5 days | Cancelled so stale mail can't fire; prospect re-drafted fresh | -- |
| Discovery runs dry everywhere | Rotates cities, then tells you -- **never lowers standards** | Broaden the ICP |
| Bounce rate >= 5% / complaints >= 8% / 3 sends fail on our side | **PAUSES ALL SENDING**, one critical alert | Fix cause, then `autopilot_ctl resume --ack` |
| **Goal reached / deadline passed** | Stops new outreach, cancels unsent drafts, tells you once | Set a new goal or `goal extend` |
| **Behind pace** (after the first week, projected from customers won) | One warning with the numbers; clears itself when back on pace | Read the bottlenecks, or extend |
| Prospect-source outage (OpenStreetMap down/blocked) | ONE warning with the real cause, exponential backoff, closes itself on recovery | Nothing, unless it persists |
| Autopilot switched OFF | Plans nothing, sends nothing (approved tasks wait) | `enable` when ready |

## 7. The brakes (all enforced in code, all tested)

- **Daily cap:** 30 maximum (`OUTBOUND_HARD_CEILING`), warm-up 10 -> 20 -> 30, counted across *all* workers via a database lock -- no restart or second worker can exceed it.
- **Pacing:** sends are spread across the send window (default Mon-Fri 9-17 in *the prospects'* timezone) with minimum gaps -- never a burst.
- **Qualification gate:** hard rules + a minimum score, re-checked at the moment of sending. There is no quota: 8 qualified prospects means 8 emails, even if the cap is 30.
- **Never twice:** one first contact and one follow-up per address; one contact per company domain per 30 days; opt-outs and bounces suppressed company-wide forever.
- **Compliance:** physical address and opt-out line appended by code to every email; the model can't remove them. Copy is validated (no links except yours, no spam phrases, no invented familiarity, no banned claims) *after* the model writes it -- a failed draft falls back to the plain template.
- **Agents can't touch the brakes:** the switch, the ledger and the suppression list live in tables the agents' database role cannot read or write.

## 8. Commands

```
autopilot_ctl status | readiness | enable [--mode auto --ack-auto] | disable | resume --ack
autopilot_ctl set daily_cap=20 timezone=America/Chicago start_hour=9 end_hour=17
autopilot_ctl approvals list | approve --all | approve ID | reject ID
autopilot_ctl goal set --customers N --days D | goal status | goal extend --days N
autopilot_ctl leads | meeting EMAIL [--external] | customer EMAIL [--external] | report [--period today|daily|weekly]
autopilot_ctl suppress add email|domain VALUE | suppress list
autopilot_ctl tick            # force one planning pass right now
```
(all take `--product xflow`). The dashboard's top strip shows workers/queue/incidents live.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `enable` says "not ready" (often: no goal set) | Run `autopilot_ctl readiness`; each BLOCKING line says what to fix. |
| Status "PAUSED by safety breaker" | Read the incident (cause is stated). Fix it, then `resume --ack`. Common: stale scraped addresses (bounces) -> tighten `icp.qualification`; SMTP password changed. |
| Nothing is being drafted | `status`: pipeline empty? discovery exhausted? `qualification` too strict? The report's "Why prospects were not contacted" lists the top failing checks. |
| Approved but not sent | Outside the send window, cap reached, or pacing -- all normal; `status` shows today's count and when the window next opens. |
| Many "failed site_reachable" | Their sites block bots/are down. Not fixable by loosening the gate; consider a different source/region. |
| Replies not appearing | Inbound process not running (`/ops/status`), or IMAP credentials wrong (a "Inbound reply check failed" incident is raised). |
| Reports not arriving | Set `REPORT_EMAIL_TO` + SMTP (or Slack); otherwise they print to the worker's console and are stored (`autopilot_ctl report`). |

## 10. Known limits -- read these

- **Live integrations are unproven from the build sandbox** (no real network there). Sections 4's dry-run and first-day approval mode are the safeguard; budget a day for surprises.
- **Email discovery is modest.** The free scraper finds addresses published on a business's own site -- mostly `info@`/`contact@` role mailboxes, which convert worse than named owners. Expect fewer qualifying prospects than a paid data vendor would give; the report will show your actual pass rate.
- **OpenStreetMap coverage varies** by city and trade, and its public servers expect low, polite usage (the system rate-limits and identifies itself with your mailbox as the contact).
- **Cold email is a low-conversion channel.** Typical reply rates are low single digits; at <= 30/day, reaching your goal depends heavily on your offer fit and your own follow-through on replies. This system maximises *qualified, deliverable, compliant, consistent* reach -- it cannot manufacture demand.
- **One mailbox, one timezone per campaign.** Use `geographies` in one timezone, or run another product config for a different one.
- **TLS certificates are verified** for SMTP, IMAP and report mail (a private mail server's certificate: `TLS_CA_FILE`). `ALLOW_INSECURE_TLS` exists for local development only and is refused in production.
- **No DKIM verification** (selectors can't be discovered via DNS) -- confirm it in your mail provider, and send a test to a Gmail account and check "Show original" for `dkim=pass`.
- **Auto mode is opt-in and gated**; leave it off until the approval-mode weeks look clean.
- Legal compliance (CAN-SPAM; GDPR/PECR if you email anyone in the UK/EU, where cold B2B email to individuals is far more restricted) is **your responsibility**. The tooling enforces the mechanics, not the legal judgement. Target US-based businesses unless you have taken advice.
