# ADR 009 — Operations Dashboard with Pending Approval Actions

**Status:** Accepted  
**Date:** 2026-04-03

## Context

Operators need visibility into the discrepancy agent's activity and a low-friction way to approve or reject pending inventory mutations. The existing approval UX requires constructing a `curl` command with the correct `run_id`, JSON body, and endpoint URL — this creates operational friction and risks typos in mutation payloads.

The Slack interactive approval path (ADR 007) handles mobile/on-call use; the web dashboard addresses desktop/operator-station use cases and provides aggregate health metrics.

## Decision

Expose two endpoints from a `dashboard` FastAPI router:

- `GET /api/dashboard/stats` — queries Postgres `DiscrepancyAuditLog` and returns JSON (7-day window): `total_processed`, `pending_approvals`, `approval_rate_pct`, `avg_discrepancy_pct`, `by_action`, plus a `pending_items` list (up to 50 items) containing run metadata for the approval table
- `GET /dashboard` — serves an inline single-page HTML dashboard

**Dashboard features:**
- 4 stat cards: Events Processed, Pending Approvals, Approval Rate, Avg Discrepancy %
- **Pending Approvals table** — shows SKU, discrepancy %, expected/actual qty, proposed action, queue age, and Approve/Reject buttons
- Action type breakdown table (7-day)
- Auto-refresh every 30 seconds; approval actions update optimistically and remove the row on success
- Approve/Reject buttons POST to `POST /api/approvals/{run_id}` with `reviewer_id: "dashboard"`

**Technology choices:** Vanilla JS + inline CSS — no build step, no CDN dependency, no npm.

## Alternatives Considered

| Option | Rejected because |
|--------|-----------------|
| Slack-only approvals (ADR 007) | Slack is async; operators at a workstation benefit from a synchronous list view |
| Dedicated approval admin panel (React) | Build toolchain overhead for a low-frequency operator action |
| CLI tool | Not accessible to non-technical stakeholders reviewing metrics |
| External BI tool (Retool, Metabase) | Adds external service dependency; auth complexity |

## Consequences

- **Positive:** Operators can approve/reject pending mutations from a browser without constructing API calls
- **Positive:** Pending approvals table with queue age enables SLA monitoring
- **Positive:** Single endpoint (`/dashboard`) replaces `curl` for day-to-day operations
- **Negative:** No authentication on `/dashboard` — suitable for internal network / VPN only
- **Negative:** Approval via dashboard always uses `reviewer_id: "dashboard"`; no per-user attribution at the HTTP level (Slack approvals preserve Slack user identity)
