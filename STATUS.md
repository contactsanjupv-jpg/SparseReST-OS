# Build status

States: NOT STARTED -> IN PROGRESS -> TESTING -> VERIFIED -> COMPLETE.
Nothing is marked VERIFIED without a passing automated test; nothing is
marked COMPLETE while a known gap in this doc still applies to it.

## Phase 1 -- Foundation
| Item | Status |
|---|---|
| Company/product/agent registry schema | VERIFIED |
| Row-Level Security (product isolation, read + write) | VERIFIED |
| Least-privileged `app_role` DB connection | VERIFIED |
| Context object (`company_id`/`product_id`/`agent_id`/`task_id`/`workflow_id`) | VERIFIED |
| Seed script (idempotent) | VERIFIED |

## Phase 2 -- Agent runtime
| Item | Status |
|---|---|
| BaseAgent lifecycle (claim -> run -> complete/fail/retry/needs_approval) | VERIFIED -- including the fix where a task genuinely pauses at 'needs_approval' rather than silently reading 'done' |
| Pluggable LLM client (Fake + real Claude) | TESTING (Fake path only -- real Claude path not exercised without a live key) |
| Permission-checked, audit-logged tool execution | VERIFIED |
| Durable job queue (FOR UPDATE SKIP LOCKED) | VERIFIED, including real concurrency (5 threads racing for 5 tasks, zero duplicates) |
| Retry with exponential backoff | VERIFIED |
| Stuck-job reaper | VERIFIED, scheduled inside `run_worker_loop` |
| Incident creation + notification on terminal failure | VERIFIED (ConsoleNotifier by default; SlackWebhookNotifier via `SLACK_WEBHOOK_URL`, not tested against a real webhook) |
| Approve/reject actually resumes/fails the paused task | VERIFIED -- approving requeues the task for the worker; rejecting fails it with a clear reason |
| **All 17 department agents** | VERIFIED, all against stub tools/adapters |
| **Complete revenue pipeline**: Prospecting -> Sales -> Follow-up -> founder-confirmed Customer -> Customer Success onboarding | VERIFIED end-to-end |
| Product-scoped agent status (same agent, different product, different current task) | VERIFIED -- the concrete proof the Company World spec depends on |
| GitHub, test-runner, deploy tool adapter interfaces | VERIFIED (stub implementations only) |
| Feedback -> roadmap triage | VERIFIED |

## Phase 3 -- Playground
| Item | Status |
|---|---|
| `/health`, `/products` | VERIFIED |
| `/tasks` (submit, list, get, retry, cancel, stream, mark-customer) | VERIFIED |
| `/approvals` (list, decide -- now actually resumes tasks) | VERIFIED |
| `/agents` (list, inspect -- product-scoped) | VERIFIED |
| `/incidents` (list, resolve) | VERIFIED |
| Streaming live execution trace (SSE) | VERIFIED |
| API auth (`FOUNDER_API_KEY`) + CORS for the frontend | VERIFIED |
| **Frontend** (`apps/web`, Next.js): product switcher, live agent grid, agent inspector, task feed + goal submission + SSE streaming, approval queue, incident feed | VERIFIED -- built, started, confirmed reaching the real backend over real HTTP with working CORS |
| 3D/2.5D visual "Company World" (agents as characters in a scene) | NOT STARTED -- a distinct follow-up on the same real backend, not required for the frontend above to be genuinely functional |

## Phase 4 -- Revenue engine
| Item | Status |
|---|---|
| Core loop (CEO -> Prospecting -> Sales -> Follow-up -> Customer) | VERIFIED end-to-end, stub tools, over both direct dispatch and real HTTP |
| Real $0-cost enrichment (OpenStreetMap discovery + public-website contact scraping) | VERIFIED via mocked HTTP responses (`ENRICHMENT_MODE=free`) -- live connectivity not testable from this sandbox, smoke-test steps in docs/ENVIRONMENT.md |
| Real $0-cost outreach (your own domain mailbox via SMTP, CAN-SPAM compliance built in) | VERIFIED via mocked SMTP (`OUTREACH_MODE=smtp`) -- same live-connectivity caveat |
| CAN-SPAM compliance (physical address gate, opt-out enforcement, suppression) | VERIFIED -- hard technical gates, not agent judgment |
| **Real $0-cost inbound reply handling** (IMAP polling of the same mailbox -- works with Gmail/Workspace) | VERIFIED via mocked IMAP -- matches replies to real prospects by email, creates real classify_reply tasks, reports real incidents on failure or unmatched replies |
| Hot-lead escalation (`high_intent`/`interested`) | VERIFIED -- now a real, trackable, resolvable Incident + real notification (console/Slack), not just an activity-feed entry |
| Meeting-booked / customer conversion, founder-confirmed only | VERIFIED -- `mark_meeting_booked` alongside the existing `mark_customer`; neither is ever agent-triggered |
| Your existing lead-gen tool, wired in as EnrichmentTool | NOT STARTED -- **needs the repo URL**; the free OSM-based tool is a real, immediately-usable option in the meantime, not just a placeholder |
| XFlow's real ICP/positioning/pricing config | BLOCKED -- **needs your confirmation**; strong evidence now points to "home-service trades lead-tracking tool," not "real-estate Zillow tool" (see chat) |

## Phase 5 -- Observability
| Item | Status |
|---|---|
| Incident data model + creation pipeline | VERIFIED |
| Automatic reaper scheduling | VERIFIED |
| Structured JSON logging with bound context + secret redaction | VERIFIED |
| Funnel-bottleneck baseline (cohort conversion vs. own trailing baseline; de-duplicated degradation Incidents) | VERIFIED |
| `/health/live`, `/health/ready` (migrations check), `/ops/status`, `/ops/workers` | VERIFIED |

## Phase 6 -- Production hardening
| Item | Status |
|---|---|
| Founder login (scrypt, sessions, lockout, TOTP) | VERIFIED |
| Rate limiting, body cap, CORS/host/headers, fail-closed prod auth | VERIFIED |
| Encrypted secrets store + `secret://`/`file://` references, key rotation | VERIFIED |
| Credential tables revoked from `app_role` | VERIFIED |
| Multi-worker config: heartbeats, graceful shutdown, singleton inbound + maintenance (advisory locks), Docker + systemd | VERIFIED (logic); container images/units NOT built or run in this sandbox |
| Crash-safe, company-level inbound processing (Message-ID idempotency) | VERIFIED |
| 2.5D Company World (isometric SVG, state derived only from real rows) + login UI + status strip | BUILT, `next build` passes, API feed VERIFIED by test; the rendered visuals were NOT visually inspected in a browser here |
| Production pre-flight (`scripts/check_production.py`) | VERIFIED |

## Phase 7 -- Acquisition engine
| Item | Status |
|---|---|
| Separate test database (suite can never touch the company DB; guard + rebuild per run) | VERIFIED |
| Autopilot ON/OFF + approval/auto modes, readiness gate, safety-breaker pause/resume | VERIFIED |
| Pipeline: discover -> ingest gate -> website research -> final gate -> personalised draft -> approval -> governed send | VERIFIED (simulated end to end with real DB/worker; network edges faked) |
| Hard qualification gate, no quota (8 qualify -> 8 sent) | VERIFIED |
| Outbound governor: cap + warm-up, window, pacing, dedup, suppression, domain cooldown, cross-process locking | VERIFIED |
| Bounce parsing, hard-bounce suppression, circuit breakers | VERIFIED |
| Reply monitoring, classification (rules + optional LLM), one follow-up, hot-lead escalation + SLA re-alert | VERIFIED |
| LLM copywriting/classification with validated deterministic fallback | VERIFIED against a scripted LLM; real Claude calls NOT exercised |
| Daily/weekly growth reports (metrics, bottlenecks, delivery email/Slack/console) | VERIFIED |
| Free adapters (OSM discovery, website research, SMTP, IMAP, DNS) wired by config | BUILT + mocked; **live connectivity unverified** (no network in sandbox) |
| Runbook, config template, founder CLI | DONE (`docs/RUNBOOK.md`) |

## What is genuinely left (inputs only you can provide)
See the closing list in chat: ICP confirmation, SMTP/IMAP mailbox + physical postal address, deployment host/domain,
founder email, optional Slack webhook / Anthropic key / GitHub token. No remaining item is blocked on more building.
