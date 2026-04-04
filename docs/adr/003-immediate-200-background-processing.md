# ADR-003: Return HTTP 200 Immediately and Process in Background

**Status**: Accepted  
**Date**: 2026-04-03

## Context

`POST /api/discrepancies/detect` starts a multi-step LangGraph workflow that makes
multiple Shopify API calls and one Claude LLM call before reaching the interrupt point.
This can take 5–30 seconds. Shopify webhook delivery additionally requires a sub-5-second
response.

## Decision

Use FastAPI `BackgroundTask` for all workflow executions. Endpoints return
`{"run_id": ..., "status": "investigating"}` immediately. The workflow runs
asynchronously. Status is polled via `GET /api/discrepancies/{run_id}`.

## Consequences

**Positive**:
- Shopify webhook delivery deadlines are met without risk.
- The operator UI gets an immediate `run_id` without waiting for investigation.
- Pattern is consistent with the order exception agent, reducing cognitive overhead
  across both projects.

**Negative**:
- In-process background tasks are lost on process crash. For the inventory agent this
  is more consequential than for the order exception agent — a lost investigation may
  mean a genuine discrepancy goes unresolved. If reliability requirements increase,
  migrate to a durable task queue (ARQ, Celery).
- `app.state.proposal_cache` is in-memory and lost on restart, making
  `GET /api/discrepancies/{run_id}` return 404 for completed investigations after a
  restart until `AsyncRedisSaver` migration is complete.
