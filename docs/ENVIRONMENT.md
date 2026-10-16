# Environment variables

Cross-checked against packages/core/config.py -- every variable the code
actually reads is listed here, and everything listed here is actually read.
If this file and config.py ever disagree, config.py is the bug (or this file
is stale) -- fix whichever is wrong before merging.

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | Yes (has a local dev default) | `postgresql+psycopg://postgres:postgres@localhost:5432/sparserest_dev` | Admin/migration connection. Used by Alembic and scripts/seed.py only. Must be a superuser or table-owner connection -- RLS does not apply to it, by design. |
| `APP_DATABASE_URL` | Yes (has a local dev default) | `postgresql+psycopg://app_role:change_me_in_env@localhost:5432/sparserest_dev` | The RLS-enforced connection every agent/tool query actually runs through. **Change the password before any non-local deployment** -- the migration creates `app_role` with the literal placeholder password `change_me_in_env`. |
| `ANTHROPIC_API_KEY` | No | unset | When set, agents use real Claude reasoning (ClaudeClient). When unset, the runtime falls back to FakeLLMClient -- deterministic, $0-cost, used automatically in all tests. |
| `ANTHROPIC_MODEL_DEFAULT` | No | `claude-sonnet-5` | Model used for most agent reasoning. Check docs.claude.com for current model strings before relying on this in production. |
| `ANTHROPIC_MODEL_CHEAP` | No | `claude-haiku-4-5-20251001` | Model used for classification/high-volume work. |
| `ANTHROPIC_MODEL_STRONG` | No | `claude-opus-5` | Model used for CEO-level synthesis. |
| `OUTREACH_PROVIDER_API_KEY` | No | unset | When set, wire in a real OutreachProvider implementation (Smartlead/Instantly-class). When unset, LoggingOutreachProvider is used -- no real email is ever sent. |
| `OUTREACH_PROVIDER_WEBHOOK_SECRET` | No | unset | For verifying inbound reply webhooks once a real provider is wired in. Not yet used by any code (no webhook receiver exists yet -- Phase 4 follow-up). |
| `SLACK_WEBHOOK_URL` | No | unset | When set, critical/error incidents are posted to this Slack incoming webhook (`SlackWebhookNotifier`). When unset, `ConsoleNotifier` is used -- incidents are still recorded to the `incidents` table either way; this only controls whether anyone gets paged. |
| `FOUNDER_API_KEY` | No | unset | A service credential for scripts/CI (constant-time compared; min 32 chars in production). The web app does NOT use it -- founders sign in with an account (`scripts/create_founder.py`). With neither a key nor an account configured, dev mode is open and production mode fails closed (503). May be a `secret://` reference. |
| `ENRICHMENT_MODE` | No | `stub` | `stub` (deterministic, $0, no network -- what every automated test relies on) or `free` (real OpenStreetMap-based discovery + public-website contact scraping, packages/tools/free_enrichment.py -- $0 cost, no API key, but real network calls). |
| `OUTREACH_MODE` | No | `stub` | `stub` (LoggingOutreachProvider -- never sends) or `smtp` (real sends via your own mailbox, packages/tools/outreach.py::SMTPOutreachProvider -- only activates once SMTP_HOST/USERNAME/PASSWORD/FROM_EMAIL are all set; falls back to stub if any are missing). |
| `SMTP_HOST` | No (required if `OUTREACH_MODE=smtp`) | unset | Your own domain's mail server, e.g. the one behind your existing email hosting. Not a new paid service -- see docs/SECURITY.md for why a transactional ESP (Resend etc.) is NOT used for this. |
| `SMTP_PORT` | No | `587` | Standard STARTTLS submission port. |
| `SMTP_USERNAME` | No (required if `OUTREACH_MODE=smtp`) | unset | Your mailbox's SMTP username (often the full email address). |
| `SMTP_PASSWORD` | No (required if `OUTREACH_MODE=smtp`) | unset | An app-specific password if your provider supports one (recommended over your real account password). |
| `SMTP_FROM_EMAIL` | No (required if `OUTREACH_MODE=smtp`) | unset | The address prospects will see and reply to. |
| `SMTP_FROM_NAME` | No | `""` | Display name on outgoing mail. |
| `SMTP_DAILY_SEND_CAP` | No | `30` | Hard cap on sends per day -- protects your sending domain's reputation. Raise deliberately, not by default. |
| `IMAP_HOST` | No (required for inbound reply checking) | unset | Your mailbox's IMAP server. Without this, replies must be classified manually via the `classify_reply` action/API. |
| `IMAP_USERNAME` | No | falls back to `SMTP_USERNAME` | Only set separately if inbound uses a different mailbox than outbound. |
| `IMAP_PASSWORD` | No | falls back to `SMTP_PASSWORD` | Same fallback logic. |
| `ENVIRONMENT` | No | `development` | Set to `production` to enable strict mode: JSON logs, HSTS, API docs disabled, fail-closed auth, 0600 master-key-file enforcement. |
| `LOG_LEVEL` | No | `INFO` | Log verbosity for every process (`DEBUG`/`INFO`/`WARNING`/`ERROR`). |
| `LOG_FORMAT` | No | `auto` | `auto` (JSON lines when `ENVIRONMENT=production`, readable console otherwise), `json`, or `console`. Every line carries request/task/agent/product ids; secrets are redacted. |
| `ALLOWED_ORIGINS` | **Yes in production** | `http://localhost:3000` | Comma-separated CORS origins. Set to your real frontend origin(s); `scripts/check_production.py` fails if it is still the localhost default. |
| `TRUSTED_HOSTS` | **Yes in production** | `*` | Comma-separated Host headers the API answers to (blocks Host-header attacks). `*` is dev-only. |
| `TRUST_PROXY_HEADERS` | No | `false` | Honour `X-Forwarded-For` (last entry only) for rate-limit client identity. Enable ONLY when the API is behind your own reverse proxy. |
| `MAX_REQUEST_BODY_BYTES` | No | `1000000` | Requests larger than this get 413. |
| `RATE_LIMIT_ENABLED` | No | `true` | Per-client token-bucket limiter (per API process). Production pre-flight fails if disabled. |
| `RATE_LIMIT_PER_MINUTE` | No | `120` | Sustained requests/minute per client (burst equals this value). `/health*` is exempt. |
| `LOGIN_RATE_LIMIT_PER_MINUTE` | No | `10` | Stricter per-IP limit on `POST /auth/login`. |
| `SESSION_TTL_HOURS` | No | `12` | Founder session lifetime. Sessions are server-side and revocable. |
| `LOGIN_MAX_FAILURES` | No | `5` | Failed logins before the account locks. Persisted in Postgres, so it holds across restarts and every API process. |
| `LOGIN_LOCKOUT_MINUTES` | No | `15` | Lockout duration. Operator unlock: `python -m scripts.create_founder EMAIL --unlock`. |
| `REQUIRE_LOGIN_IN_PRODUCTION` | No | `true` | When true and `ENVIRONMENT=production`, the API answers 503 (fails closed) until a founder account or `FOUNDER_API_KEY` exists. |
| `SECRETS_MASTER_KEY_FILE` | Needed for `secret://` | unset | Path to a file (mode 0600 in production -- enforced) holding the Fernet master key that unlocks the encrypted secrets store. Preferred over the env var. Generate: `python -m scripts.secrets_cli gen-key`. |
| `SECRETS_MASTER_KEY` | Alternative | unset | The master key itself. Comma-separated list supported for rotation (first key encrypts; all decrypt). Prefer the `_FILE` form. |
| `DNS_VALIDATION` | No | `true` | Real DNS lookups (MX for prospect email domains, SPF/DMARC for your sending domain). The qualification gate FAILS CLOSED when a domain cannot be verified, so only turn this off for offline development. |
| `OUTBOUND_HARD_CEILING` | No | `30` | Absolute maximum emails per day the system will ever send, regardless of autopilot configuration (`daily_cap` is validated against it). Raise it deliberately, and only after warm-up and good deliverability. |
| `REPORT_EMAIL_TO` | No | unset | Where daily/weekly growth reports are emailed (sent through your own SMTP mailbox -- not counted against the outreach cap). With neither this nor `SLACK_WEBHOOK_URL`, reports are stored and printed to the console only. |
| `APPROVAL_REMINDER_HOURS` | No | `12` | An outreach draft waiting for your approval longer than this raises ONE (de-duplicated) warning incident; it clears itself when you approve. |
| `HOT_LEAD_SLA_HOURS` | No | `4` | A hot-lead incident still open after this long is re-escalated once at critical severity. |
| `TLS_CA_FILE` | No | unset | Extra CA bundle (PEM) trusted for SMTP/IMAP connections -- for a private mail server or a local test server with its own certificate. The system trust store is always used too. |
| `ALLOW_INSECURE_TLS` | No | `false` | **Development only.** Disables certificate and hostname verification on every mailbox connection. Refused when `ENVIRONMENT=production`; `check_production` fails if it is on. |
| `WORKER_POLL_INTERVAL_SECONDS` | No | `1.0` | Idle sleep between queue polls in `scripts.run_worker`. |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | No | `15` | How often a worker/inbound process records liveness. |
| `WORKER_STALE_AFTER_SECONDS` | No | `60` | A process silent this long is reported as not alive by `/ops/workers`. Keep it well above the heartbeat interval. |
| `INBOUND_POLL_INTERVAL_SECONDS` | No | `60` | Mailbox poll interval (backs off automatically while the mailbox is failing). |
| `MAINTENANCE_INTERVAL_SECONDS` | No | `300` | Housekeeping cadence (stuck-task reaper, session purge, funnel-degradation check). One worker at a time runs it. |

## Never set these to real values in source control
`.env` is gitignored. `.env.example` (below) contains only placeholders.

## Enabling the real $0-cost tools -- and verifying them yourself

This sandbox's network egress is restricted and cannot reach OpenStreetMap,
arbitrary business websites, or any real SMTP server -- so `ENRICHMENT_MODE=free`
and `OUTREACH_MODE=smtp` have been tested with mocked HTTP/SMTP clients
(tests/test_free_enrichment.py, tests/test_smtp_outreach.py), never live,
from within this environment. That proves the request-building and
response-parsing logic; it does not prove real-world connectivity. Once
you're running this locally:

**Enrichment smoke test** (no email account needed):
```bash
ENRICHMENT_MODE=free python -c "
import httpx
from packages.tools.free_enrichment import FreeEnrichmentTool
tool = FreeEnrichmentTool(httpx.Client())
leads = tool.find_and_enrich(icp={'industries': ['plumbing'], 'geography': 'Indianapolis, IN'}, limit=5)
for l in leads: print(l)
"
```
If this returns real business names (even with `contact_email=None` for
most -- OSM/website coverage is real but incomplete), the live path works.

**Outreach smoke test** (sends one real email to yourself -- costs nothing,
uses a mailbox you already own):
```bash
OUTREACH_MODE=smtp SMTP_HOST=... SMTP_USERNAME=... SMTP_PASSWORD=... SMTP_FROM_EMAIL=... python -c "
from packages.tools.outreach import SMTPOutreachProvider
p = SMTPOutreachProvider(host='...', port=587, username='...', password='...', from_email='...')
print(p.send(to_email='YOUR_OWN_EMAIL_HERE', subject='SparseReST test', body='It works.'))
"
```

**Inbound smoke test** (reads real unread mail from your own inbox -- reply
to the test email above from a different address first, so there's
something to find):
```bash
python -c "
from packages.tools.imap_inbound import ImapInboundChecker
checker = ImapInboundChecker(host='imap.example.com', username='...', password='...')
for reply in checker.fetch_unread_replies():
    print(reply)
"
```

## Gmail / Google Workspace configuration example

`SMTPOutreachProvider` and `ImapInboundChecker` are plain SMTP/IMAP, so any
provider works, including a Gmail or Google Workspace account you already
have (not a new paid service). You'll need an **app password**, not your
regular account password (Google Account settings -> Security -> App
passwords -- requires 2-Step Verification to be enabled):

```bash
OUTREACH_MODE=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@yourdomain.com
SMTP_PASSWORD=<the 16-character app password>
SMTP_FROM_EMAIL=you@yourdomain.com

IMAP_HOST=imap.gmail.com
# IMAP_USERNAME / IMAP_PASSWORD fall back to the SMTP ones above automatically
```

**Gmail-specific note**: Gmail's own sending limits (500/day for regular
accounts, higher for Workspace) are far above `SMTP_DAILY_SEND_CAP`'s
default of 30 -- the cap here is about protecting deliverability
reputation for a new sending pattern, not about hitting Google's ceiling.
Google's own terms also prohibit bulk/abusive sending; the CAN-SPAM
compliance built into SalesAgent (physical address, opt-out, suppression --
see docs/SECURITY.md) is what keeps real cold outreach through a personal
or Workspace account from looking like abuse in the first place.

## Inbound reply checking -- how it actually runs

`packages/core/inbound.py::run_inbound_check_loop` is a separate long-lived
loop, the same shape as `packages/core/orchestrator.py::run_worker_loop` --
run it as its own process (or call `process_inbound_replies()` once per
cron tick) alongside the task worker, not instead of it:

```python
from packages.core.inbound import build_inbound_checker, run_inbound_check_loop

checker = build_inbound_checker()
if checker:
    run_inbound_check_loop(company_id, xflow_product_id, checker, poll_interval_seconds=60)
```
