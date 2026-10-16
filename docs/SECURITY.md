# Security model -- what's actually implemented in this checkpoint

This describes what's built and verified by tests today. For the full target
security model, see docs/ARCHITECTURE.md Section 8 -- this file only claims
what's real right now.

## Product isolation (verified by tests/test_rls_isolation.py)

- Every product-scoped table has Postgres Row-Level Security enabled and
  **forced** (`FORCE ROW LEVEL SECURITY` -- applies even to the table owner
  querying through a non-superuser role).
- The application never connects to Postgres as the table owner or a
  superuser for agent/tool queries -- it connects as `app_role`, a
  least-privileged role created by the RLS migration, granted only
  SELECT/INSERT/UPDATE/DELETE (no DDL, no role management).
- Every query an agent/tool makes goes through `scoped_session(ctx)`
  (packages/db/scoped_session.py), which sets the Postgres session variables
  the RLS policies key off of, derived from the calling agent's actual
  `is_company_level` flag in the `agents` table -- never from a caller-
  supplied flag that could be spoofed.
- A company-level agent (CEO, Finance, Strategy, etc.) can read rollups
  across products; a product-level agent (Prospecting, Sales, etc.) is
  refused outright (a raised `ValueError`, not a silent empty result) if its
  context has no `product_id`.
- Writing into a specific product's execution data (prospects, funnel
  events, experiments, product config) always requires that exact product's
  scope, even for company-level agents -- there is no write bypass for
  execution-data tables, only for the delegation/logging tables (tasks,
  agent_runs, activity_events, audit_log) where a company-level agent
  legitimately creates work for a product-level one.

## Least-privilege tool access (verified by tests/test_revenue_cycle.py)

- Every tool call goes through `packages/tools/execution.py::call_tool`,
  never invoked directly by agent code.
- An agent may only call tools belonging to its own department (plus a small
  set of company-wide read-only tools). A denied call is recorded to
  `audit_log` with `result='denied'` -- not silently dropped.
- A product-level agent additionally needs `product_agent_enablement.enabled
  = true` for the specific product in its context.

## Audit trail

- Every tool call (success, error, or denial) is written to `audit_log` with
  the full context (`company_id`, `product_id`, `agent_id`, `task_id`,
  `workflow_id`), input, output, and result -- written by the tool wrapper
  itself, so individual agent implementations cannot forget to log.
- `audit_log` is append-only in practice (no code path updates or deletes
  rows); it is not yet enforced as append-only at the database level
  (no `REVOKE UPDATE, DELETE` applied) -- tracked as a follow-up.

## Approval gates

- `ApprovalService` creates a pending `Approval` row before any outreach
  send; the Sales Agent's send tool only actually calls the outreach
  provider once that approval is marked `approved`.
- Approvals are not yet scoped by product in the RLS sense (see the RLS
  migration's docstring) -- documented as a known gap, not silently assumed
  covered.

## Known gaps (not yet built, listed honestly rather than omitted)

- API auth exists (`FOUNDER_API_KEY` -- see docs/ENVIRONMENT.md) but is a single
  shared key, not a real login/session system; still fine for a single-founder
  tool, not fine if more than one person ever needs distinct access.
- No rate limiting on the API yet.
- `LOG_LEVEL` is read from config but not yet wired into real structured
  logging.
- No secrets manager integration yet -- `credentials_ref` in
  `product_integrations` is a documented column, not yet backed by a real
  secrets store (env vars only, for now).
- Prompt-injection defenses are a design principle (external content is
  data, never instructions -- see docs/ARCHITECTURE.md Section 8) but no
  agent that consumes untrusted external content (a scraped page, an inbound
  reply) has been built yet to actually exercise this.
- The `/tasks/{id}/stream` SSE endpoint is polling-based (see
  apps/api/streaming.py) -- fine at this system's actual scale, not yet
  load-tested at any real concurrency.
- CORS is currently open to `http://localhost:3000` only (the frontend's dev
  origin) -- correct for local dev, needs tightening to a real deployed
  origin before this runs anywhere else.
- The frontend (`apps/web`) carries `FOUNDER_API_KEY` via a `NEXT_PUBLIC_*`
  env var, which is bundled into client-side JavaScript and visible to
  anyone with browser devtools -- an accepted, documented tradeoff for
  localhost-only, single-founder use (see `apps/web/lib/api.js`), not safe
  for any real deployment.
- `ApprovalService.decide` writes directly to the `tasks` table via the
  admin connection (bypassing that table's RLS) when resuming/failing a
  task after a founder decision -- deliberate, since an approve/reject
  decision is a trusted, already-authorized action (the same category as
  the job queue's claim step), and no agent can call this method itself,
  only the founder-facing API.

## Durable job queue (verified by tests/test_worker_queue.py)

- Dequeue uses Postgres `FOR UPDATE SKIP LOCKED`, verified under real
  concurrency: `test_concurrent_workers_never_claim_the_same_task` runs 5
  threads racing to claim from a queue of 5 tasks and asserts every task was
  claimed exactly once, with no duplicates and none missed. This is the
  actual proof behind "prevent duplicate execution," not just a design claim.
- A failing task is retried with exponential backoff (5s, 10s, 20s, ...,
  capped at 300s) up to its `max_attempts`; only once attempts are exhausted
  does it fail terminally.
- A terminal failure creates a `critical` Incident (via `IncidentService`)
  and notifies through the configured `NotificationAdapter` (a $0-cost
  console/log stub by default -- see docs/ENVIRONMENT.md for wiring a real
  one). Verified: `test_retry_exhaustion_creates_critical_incident_and_notifies`.
- A worker that crashes mid-task leaves its lock behind; `requeue_stuck_tasks`
  (a reaper, not yet scheduled to run automatically -- see STATUS.md) detects
  locks older than a threshold and either requeues (attempts remain) or fails
  terminally (attempts exhausted) -- verified for both branches.

## Cold outreach compliance (CAN-SPAM) and the $0-cost provider choice

Researched before building, not assumed: **Resend, SendGrid, Mailgun,
Postmark, and effectively every transactional/marketing ESP explicitly
prohibit cold outreach in their Acceptable Use Policies.** Confirmed against
Resend's actual AUP specifically because XFlow already uses Resend for its
own transactional email -- routing cold prospecting through that same
account would risk losing it entirely, including XFlow's password-reset and
receipt email. `SMTPOutreachProvider` (packages/tools/outreach.py) sends
through the founder's own domain mailbox directly instead, which is not
bound by a third-party ESP's terms in the same way.

That doesn't mean anything goes: US B2B cold email is governed by CAN-SPAM,
which is opt-out-based (no prior consent required, unlike GDPR/CASL), but
does require, in every commercial email:
- A truthful From/Reply-To and a non-deceptive subject line.
- A valid physical mailing address.
- A clear, working opt-out mechanism, honored within 10 business days.

Both of the address and opt-out requirements are enforced as hard technical
gates, not relied on as agent judgment:
- `SalesAgent._draft_and_send` refuses to send at all if
  `product_config.sales.physical_address` isn't configured -- see
  ProspectingAgent's ICP guardrail for the same pattern applied elsewhere.
- Every outgoing email gets an in-body opt-out line and an RFC 8058
  `List-Unsubscribe` header (one-click, supported by most modern mail
  clients) automatically.
- A reply classified `unsubscribe` sets `prospects.do_not_contact = true`
  immediately (not within 10 days -- immediately), and `_draft_and_send`
  checks this as a hard block before every single send, including
  follow-ups -- there is no code path that can re-contact a suppressed
  prospect without a human deliberately clearing the flag.

`SMTPOutreachProvider` also defaults to a conservative 30-sends/day cap --
not a compliance requirement, but a real deliverability protection for a
sending identity with no warmup history. Tested via
tests/test_can_spam_compliance.py and tests/test_smtp_outreach.py.

**What this does NOT cover**: laws in other jurisdictions (GDPR/CASL are
opt-in, not opt-out -- this system is not built for those regimes),
state-level equivalents beyond CAN-SPAM's federal preemption, and this is
engineering-level compliance scaffolding, not legal advice -- confirm your
actual obligations with someone qualified before relying on it.

---

# Production hardening (added in the production-readiness pass)

| Control | Where | Proved by |
|---|---|---|
| Founder login: scrypt hashes, server-side revocable sessions (only SHA-256 of the token stored), optional TOTP 2FA (seed encrypted at rest) | `apps/api/auth_service.py`, `security.py` | `tests/test_founder_auth.py` |
| Brute-force defence: per-account lockout persisted in Postgres (survives restarts, shared by all API processes) + per-IP login rate limit; unknown email burns the same CPU as a real one | same | same |
| Fail-closed auth: in production with no account and no key, every protected route returns 503; deactivating the only account never reopens the API | `apps/api/auth.py` | same |
| Encrypted secrets store (Fernet, name-bound ciphertext, key rotation) + `secret://` / `file://` config references; `Settings` repr is redacted | `packages/core/secrets.py` | `tests/test_secrets_store.py` |
| Credential tables are unreachable by `app_role` (the role agents run as) | migration `a1c4e7b90d21` | `tests/test_ops_tables_isolation.py` |
| Rate limiting, request-size cap, strict CORS (no cookies => no CSRF surface), trusted hosts, security headers + HSTS, docs disabled in production | `apps/api/ratelimit.py`, `middleware.py`, `main.py` | `tests/test_rate_limit_and_middleware.py` |
| Structured JSON logs with request/task/agent/product ids; secrets, bearer tokens and URL credentials redacted; query strings never logged | `packages/core/log.py` | `tests/test_structured_logging.py` |
| Inbound mail: at-least-once + Message-ID idempotency (a crash can't drop or double-process a reply); exact-match sender lookup (the old `ILIKE` treated `_`/`%` as wildcards); body size capped | `packages/core/inbound.py` | `tests/test_workers_and_scripts.py` |
| Containers: non-root, read-only filesystem, all capabilities dropped; systemd units sandboxed | `Dockerfile`, `docker-compose.yml`, `infra/` | `tests/test_environment_docs.py` (units reference real modules) |
| Pre-flight gate refusing unsafe production config | `scripts/check_production.py` | `tests/test_workers_and_scripts.py` |

## Known limits (stated, not hidden)
* **Rate limits are per API process.** N uvicorn workers => up to N x the limit. Login lockout does not have this limit (it is in Postgres).
* **No HSM / hosted-vault audit trail.** The secrets store removes plaintext credentials from `.env` and the DB, at $0; the master key is still one file you must protect and back up. A paid vault (Vault, AWS SM) can replace `SecretStore` behind the same `resolve_reference` seam if you ever want it.
* **TOTP has no recovery codes.** Recovery path is the operator CLI (`create_founder --reset-password`), which needs shell access to the server.
* **Single founder role.** There is no multi-user permission model; every founder account is fully privileged.
* **Live connectivity (OpenStreetMap, SMTP, IMAP, Slack, Claude) has never been exercised from this sandbox** -- only mocked. See the smoke tests in `ENVIRONMENT.md`.
* **Agents still run stub GitHub/CI/deploy adapters.** There is no free equivalent of "your repository".


---

# Acquisition engine (outbound email safety)

| Control | Where | Proved by |
|---|---|---|
| **Outbound governor**: every send must first reserve a slot in a Postgres ledger under an advisory lock -- daily cap (warm-up 10/20/30, hard ceiling), send window, pacing with jitter, one first-contact + one follow-up per address, one contact per company domain per 30 days, company-wide suppression list | `packages/core/outbound.py` | `tests/test_outbound_governor.py` (incl. 8 concurrent threads racing for the last slot) |
| Switches/ledger/suppressions are admin-connection-only: `app_role` (what agents run as) has all privileges REVOKED, so an agent or an injected prompt cannot raise its cap, un-suppress someone, or un-pause sending | migration `b2d5f8a1c3e4` | `tests/test_ops_tables_isolation.py` |
| **Circuit breakers** pause ALL sending (one critical, de-duplicated incident) on hard-bounce rate, complaint/unsubscribe rate, or consecutive on-our-side send failures; resuming is an explicit founder action | `outbound.evaluate_health` | governor + workflow tests |
| **Hard qualification gate**, re-checked at the moment of sending; fails closed when a domain/site cannot be verified; no quota exists anywhere | `packages/core/qualification.py`, Sales agent | `tests/test_qualification_gate.py`, `test_acquisition_workflow.py` |
| **SSRF protection** on every fetch of a third-party URL (prospect websites come from untrusted data): public http(s) only, private/loopback/link-local/metadata addresses refused, DNS-rebinding style resolution checked, every redirect hop re-validated | `packages/tools/netsafety.py`, `research.py` | `tests/test_research_tool.py` |
| **Prompt-injection defence**: prospect-site text reaches the model only as quoted untrusted data; output is accepted only if it passes a validator (no links except the product's own, no spam phrases, no invented familiarity, no banned claims, length, must reference the prospect); the compliance footer is appended by code | `packages/core/copywriter.py` | `tests/test_copywriter.py` |
| Opt-outs: rule-based detection always wins over the LLM and cannot be downgraded by it; suppression survives re-discovery | `classify_reply` | `tests/test_reply_classification.py`, workflow tests |
| An autopilot-labelled task cannot bypass approval: the Sales agent re-verifies mode, switch, pause state and the founder's track record itself | Sales agent / `autopilot.auto_send_allowed` | `tests/test_autopilot_controls.py` |
| Test isolation: the suite runs against a dropped-and-rebuilt `*_test` database and refuses to run against anything else | `tests/conftest.py` | `tests/test_zz_isolation_guard.py` |

## Fixed in this pass (pre-existing defects found by the new tests)
* **"Not interested" was classified as *interested*** (substring match) -- rejections would have been escalated to you as hot leads. Negatives are now checked first.
* Approving a draft re-requested approval on resume, leaving orphan pending approvals; a task now holds at most one pending approval.
* The planner could draft the same prospect twice while a draft awaited approval for more than 2 days.
* Out-of-office replies ("out of the office", "automatic reply") were not recognised and could stop the follow-up path.

## Fixed after the real-integration verification
* **Mailbox TLS is now verified** (certificate chain AND hostname) for SMTP outreach, report mail and IMAP. Previously all three used unverified contexts (a network attacker could have captured the mailbox password). `TLS_CA_FILE` trusts a private CA; `ALLOW_INSECURE_TLS` is dev-only, refused in production, flagged by `check_production`. A failed handshake now surfaces its real cause instead of "Server not connected". Tests: `tests/test_tls_verification.py` (a real TLS server).
* **Dry-run sends no longer advance a prospect**, and the follow-up path independently requires a real first email in the ledger.
* **One outage = one incident**: failure incidents are de-duplicated while open; discovery failures back off exponentially and close on recovery.
* **Founder milestones are validated** against the real funnel (a prospect never emailed cannot be recorded as a customer unless the founder marks it `--external`).
* **Test isolation**: the suite ignores the developer's `.env` and process environment entirely.

## Additional known limits
* No DKIM verification (selectors are not discoverable via DNS); SPF/DMARC presence is checked.
* The send window uses one timezone per campaign (the recipients').
* `max_task_age` expiry and approval reminders assume the maintenance loop is running (it runs inside the worker).
