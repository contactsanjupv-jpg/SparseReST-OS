# SparseReST Company OS -- Phase 1 + early Phase 2 checkpoint

Status: **early build, not production, not the full system.** This is a real,
tested, running slice -- not a mockup -- but only covers part of the full
architecture in docs/ARCHITECTURE.md. See STATUS.md for exactly what's done,
tested, and not yet built.

## What actually works right now

See STATUS.md for the full living checklist, including an honest split of
what's left to build vs. what's blocked on your input (real credentials, the
lead-gen tool repo, XFlow's actual ICP). Highlights:

- Full Postgres schema via Alembic migrations, with Row-Level Security
  enforcing product isolation at the database layer through a
  least-privileged `app_role` (never the table owner).
- **All 17 department agents** from the original org chart are real,
  running on one shared runtime, each against a $0-cost stub tool/adapter
  where a real integration would otherwise be required.
- **The complete revenue pipeline**: Prospecting -> Sales (outreach,
  approval-gated) -> automatic Follow-up (for prospects gone quiet) ->
  founder-confirmed Customer conversion -> Customer Success onboarding --
  all real, all tested, all through the shared runtime.
- **Approvals actually resume the paused task** -- approving a request via
  the API requeues the real task for the worker to pick back up; rejecting
  fails it with a clear reason. (This used to be broken: an approval-gated
  task quietly reported 'done' regardless of the decision. Fixed and tested.)
- **Product-scoped agent views**: the same Sales agent genuinely shows a
  different current task depending on which product you're looking at --
  proven by test, not just documented -- and agents not enabled for a
  product don't appear in that product's view at all.
- A durable, Postgres-backed job queue (`FOR UPDATE SKIP LOCKED`) with
  exponential backoff retries, a scheduled stuck-job reaper, and
  duplicate-execution prevention verified under real concurrency.
- A structured incident/alert pipeline with pluggable notifications
  (console by default, Slack via `SLACK_WEBHOOK_URL`).
- A FastAPI control plane: `/health`, `/products`, `/tasks` (submit/list/
  get/retry/cancel/stream/mark-customer), `/approvals`, `/agents`,
  `/incidents` -- optional `FOUNDER_API_KEY` bearer auth, CORS for the
  frontend, real Server-Sent-Events execution streaming.
- **A real, running Playground frontend** (`apps/web`, Next.js) -- a
  product switcher, a live agent grid (grouped by department, colored by
  real status, click to inspect), a task feed with goal submission and live
  SSE streaming, an approval queue, and an incident feed. Built, started,
  and verified against the live backend over real HTTP with CORS -- not a
  mockup.
- 200+ automated backend tests, all passing against a real local Postgres (not SQLite -- RLS and `FOR UPDATE SKIP LOCKED` are Postgres features and a substitute would prove nothing).
  instance (not SQLite -- RLS and `FOR UPDATE SKIP LOCKED` are Postgres
  features and a substitute would prove nothing).

## What does NOT work yet

- No real LLM reasoning (FakeLLMClient/deterministic logic only, until
  `ANTHROPIC_API_KEY` is set -- see ENVIRONMENT.md).
- No real GitHub access, CI, or deploy target -- all stubbed
  (StubGitHubAdapter, StubTestRunner, StubDeployAdapter). Unlike enrichment/
  outreach, there's no free/self-hosted equivalent for "your actual
  repository" -- this genuinely needs your own GitHub token.
- **Real enrichment, outreach, AND inbound replies now exist at $0 cost**,
  off by default (`ENRICHMENT_MODE=stub`, `OUTREACH_MODE=stub` -- what every
  test relies on): `FreeEnrichmentTool` (OpenStreetMap business discovery +
  public-website contact scraping, no API key), `SMTPOutreachProvider` (real
  sends via your own domain mailbox, CAN-SPAM compliance built in as a hard
  gate), and `ImapInboundChecker` (polls that same mailbox for replies,
  matches them to real prospects, creates real classification tasks
  automatically -- no manual paste-the-reply-in step). A high-intent or
  interested reply becomes a real, trackable Incident with a real
  notification, not just an activity-feed entry. See docs/ENVIRONMENT.md for
  exactly how to turn these on, including a Gmail/Google Workspace example,
  and how to smoke-test each one yourself -- their live network calls have
  been tested against mocked responses shaped like the real services'
  documented formats, never against the real internet, since this build
  environment's network egress can't reach them.
- The pipeline's two founder-only, never-autonomous transitions --
  `mark_meeting_booked` and `mark_customer` -- complete the manual middle of
  Prospecting -> Sales -> Follow-up -> **Meeting -> Customer**, without ever
  fabricating that a real conversation or sale happened.
- The Playground frontend is a real, functional dashboard -- it is NOT the
  3D/2.5D "Company World" visual scene from the original spec (agents as
  characters walking around an office). That's a distinct, substantial
  follow-up built on top of this same real backend/event data; nothing
  about the current frontend needs to change for it to happen.
- `FOUNDER_API_KEY` auth is a single shared key, not a real login system --
  fine for one founder, not for a team. The frontend also has no login
  screen of its own; it just carries the same key.
- XFlow's real ICP/positioning/pricing is still an open question (see
  STATUS.md and the chat) -- the placeholder guardrail correctly refuses to
  run prospecting against it in the meantime, verified over real HTTP.

## Local setup (developed and tested on Ubuntu 24.04 in this environment;
adjust package-manager commands for macOS -- see the macOS notes below)

```bash
# 1. Postgres 16, running locally
#    macOS: brew install postgresql@16 && brew services start postgresql@16
createdb sparserest_dev

# 2. Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# edit .env if your local Postgres user/password differ from the defaults

# 4. Run migrations (creates schema + RLS policies + the app_role Postgres role)
alembic upgrade head

# 5. Seed the company/agent/product registry
python -m scripts.seed

# 6. Run the tests
pytest tests/ -v

# 7. Run the API
uvicorn apps.api.main:app --reload
# then: curl http://127.0.0.1:8000/health

# 8. Run the Playground frontend (separate terminal)
cd apps/web
npm install
cp .env.local.example .env.local
npm run dev
# then open http://localhost:3000 -- the API must be running (step 7) for it to show real data
```

### macOS-specific notes (not verified in this build environment -- verify locally)
- `brew install postgresql@16` and `brew services start postgresql@16` are the
  Homebrew equivalents of the `apt`/`service postgresql` commands used to
  build and test this in Linux. Not run/verified on macOS from this
  environment -- if `createdb` or `psql` aren't on your PATH after
  installing, add `$(brew --prefix postgresql@16)/bin` to it.
- Everything from step 2 onward (Python/pip/alembic/pytest/uvicorn) is
  platform-independent and should behave identically on macOS.

## Repository layout

See docs/ARCHITECTURE.md Section 11 for the target structure; this
checkpoint implements:

```
apps/api/            FastAPI control plane (health, products, tasks, approvals, agents, incidents)
apps/web/             Next.js Playground frontend -- product switcher, live agent grid, tasks/approvals/incidents
packages/core/        config, Context, domain services, orchestrator, job queue, incidents
packages/db/           SQLAlchemy models, Alembic migrations, RLS, scoped_session
packages/agents/       BaseAgent, LLM client, all 17 department agent implementations
packages/tools/        permissioned tool execution wrapper, enrichment/outreach/GitHub/test-runner/deploy interfaces
scripts/seed.py         idempotent registry seed
tests/                  54 tests, all against real Postgres
```


## Running the XFlow acquisition machine
Start with **`docs/RUNBOOK.md`** -- what to provide, how to go live safely, the 10-minute daily routine, and what to do when something breaks.
