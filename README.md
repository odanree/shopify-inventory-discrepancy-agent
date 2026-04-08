# Shopify Inventory Discrepancy Agent

An AI-powered LangGraph agent that detects inventory discrepancies in a Shopify store, investigates root causes using Claude, proposes corrections, and requires human approval before applying any changes — with Slack-native approve/reject buttons and full audit trail.

**Live demo:** `https://shopify-inventory.danhle.net/dashboard`

---

## What it does

When inventory diverges from expected levels, the agent:

1. **Detects** the discrepancy via Shopify `inventory_levels/update` webhook or the scheduled reconciliation
2. **Classifies** severity: minor (<5%), moderate (5–15%), major (15–50%), critical (>50%)
3. **Investigates** using Claude — pulls inventory levels across all locations, recent adjustments, and open orders to produce a root cause analysis
4. **Proposes** a resolution: adjust to expected, adjust to ERP baseline, transfer from another location, or hold for review
5. **Pauses** for human approval — the graph interrupts before any Shopify mutation
6. **Notifies** via Slack with Block Kit message and inline Approve/Reject buttons
7. **Applies** the correction to Shopify inventory on approval (skipped in shadow mode)
8. **Verifies** the change was applied correctly, with retry on failure
9. **Audits** every decision to PostgreSQL and Google Sheets with LLM token cost

---

## Architecture

```
Shopify webhook / scheduler
    │
    ▼
POST /api/discrepancies/detect
    │  (returns 200 immediately)
    ▼
Background task
    │
    ▼
┌─────────────────────────────────────────────┐
│             LangGraph State Machine          │
│                                              │
│  detect ──► investigate ──► propose          │
│       ──── INTERRUPT ────                    │
│  apply_mutation ──► verify ──► notify ──►    │
│  audit                                       │
│                                              │
│  (retry loop on verify failure)              │
└─────────────────────────────────────────────┘
    │
    ├──► Shopify API     (inventory adjustments, transfers)
    ├──► Slack           (interactive Block Kit approval messages)
    ├──► PostgreSQL      (audit log, token costs)
    └──► Google Sheets   (audit trail spreadsheet)
```

**Key design decisions:**
- **Interrupt-before pattern** — graph pauses before `apply_mutation`; no Shopify write happens without explicit approval (ADR 004)
- **Slack-native approvals** — Block Kit messages with Approve/Reject buttons POST back to `/api/slack/actions`; signed with HMAC to prevent spoofing (ADR 007)
- **Redis checkpointer** — graph state persists across the interrupt/resume boundary using `AsyncRedisSaver` (ADR 001)
- **Multi-location awareness** — investigate node pulls inventory across all Shopify locations to enable transfer proposals (ADR 010)
- **Shadow mode** — `AGENT_MODE=shadow` runs the full graph including the approval interrupt but skips all Shopify mutations (ADR 011)
- **LLM cost tracking** — investigate call captures token usage and USD cost; tracked per event in PostgreSQL (ADR 015)

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Agent framework | LangGraph (stateful graph, interrupt-before, Redis checkpointer) |
| LLM | Claude `claude-sonnet-4-6` via Anthropic API |
| API | FastAPI (async, background tasks) |
| Database | PostgreSQL + SQLAlchemy (async) + Alembic |
| Cache / state | Redis (idempotency + LangGraph checkpoint) |
| Notifications | Slack Block Kit (interactive) + Google Sheets |
| Deployment | Docker Compose + Caddy reverse proxy |
| CI | GitHub Actions (ruff + pytest) |

---

## Project structure

```
app/
  agent/
    graph.py       # LangGraph state machine + interrupt-before compile
    nodes.py       # Node functions (detect, investigate, propose, apply, verify, notify, audit)
    state.py       # DiscrepancyState TypedDict
    tools.py       # Shopify + audit + sheets tools (injected dependencies)
  routers/
    discrepancies.py    # Discrepancy detection endpoint
    approvals.py        # Human approval endpoint (resume workflow)
    inventory_webhook.py # Shopify inventory_levels/update handler
    slack_actions.py    # Slack interactive button callback handler
    dashboard.py        # Ops dashboard (HTML + JSON stats API)
  services/
    shopify_client.py   # Shopify Admin API wrapper (inventory + transfers)
    slack_client.py     # Slack webhook + Block Kit client
    google_sheets.py    # Google Sheets audit client
    event_router.py     # Redis pub/sub event router
    idempotency.py      # Webhook deduplication
    kill_switch.py      # Redis-backed kill switch
  models/
    db.py          # SQLAlchemy ORM models
  db/
    session.py     # Async engine + session factory
alembic/           # Database migrations
docs/
  adr/             # Architecture Decision Records (ADR 001–015)
  runbooks/        # Operational runbooks (Slack setup, shadow mode demo)
  LEARNINGS.md     # Debugging gotchas and lessons learned
scripts/
  seed_demo.py         # Send demo discrepancy payloads
  register_webhooks.py # Register webhooks with Shopify
tests/
  test_agent.py        # Node-level and graph tests
  test_approvals.py    # Approval flow tests
  test_discrepancies.py # Detection and proposal tests
```

---

## Running locally

```bash
# Install dependencies
pip install -e ".[dev]"

# Start infrastructure
docker compose up -d postgres redis

# Run migrations
alembic upgrade head

# Start the API
uvicorn app.main:app --reload

# Seed demo discrepancy events
python scripts/seed_demo.py

# Check pending approvals
curl http://localhost:8000/api/approvals/pending

# Approve a run
curl -X POST http://localhost:8000/api/approvals/<run_id> \
  -H "Content-Type: application/json" \
  -d '{"approved": true, "reviewer_id": "your-name"}'
```

Environment variables:

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL async URL |
| `REDIS_URL` | Redis connection URL |
| `SHOPIFY_SHOP_DOMAIN` | e.g. `yourstore.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | Shopify Admin API token |
| `SHOPIFY_WEBHOOK_SECRET` | HMAC secret for webhook verification |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |
| `SLACK_SIGNING_SECRET` | Slack app signing secret (for button callbacks) |
| `AGENT_MODE` | `shadow` to skip Shopify mutations |
| `ADMIN_API_KEY` | Dashboard HTTP Basic Auth password |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Path to Google service account JSON |
| `AUDIT_SPREADSHEET_ID` | Google Sheets spreadsheet ID for audit trail |

---

## Approval flow

```
Discrepancy detected
    │
    ▼
Graph runs to interrupt point
    │
    ▼
Slack message: [CRITICAL] Inventory Approval Required — SKU-XYZ
  ┌─────────┐  ┌────────┐
  │ Approve │  │ Reject │   <- click in Slack
  └─────────┘  └────────┘
    │
    ▼
POST /api/slack/actions  (signature verified)
    │
    ▼
Graph resumes: apply_mutation → verify → notify → audit
```

Or approve via API:
```bash
curl -X POST /api/approvals/<run_id> \
  -d '{"approved": true, "reviewer_id": "operator"}'
```

---

## Dashboard

The ops dashboard at `/dashboard` shows:
- Events processed (7d)
- Pending approvals with inline approve/reject
- LLM cost and token breakdown per event (7d)
- Action breakdown by resolution type

Protected by HTTP Basic Auth when `ADMIN_API_KEY` is set.

---

## Documentation

- [ADR 001–015](docs/adr/) — Architecture decisions
- [LEARNINGS.md](docs/LEARNINGS.md) — Debugging lessons (Alembic stamp gotcha, FastAPI stream consumed, etc.)
- [Runbooks](docs/runbooks/) — Slack app setup, shadow mode smoke test
