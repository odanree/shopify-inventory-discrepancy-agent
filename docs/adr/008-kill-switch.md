# ADR 008 — Redis-Backed Kill Switch for Agent Control

**Status:** Accepted  
**Date:** 2026-04-03

## Context

The inventory discrepancy agent runs automated inventory mutations and Shopify API calls triggered by webhook events. During Shopify API incidents, bad data pipelines, or merchant-requested pauses, operators need to halt all automated mutations immediately without redeploying or restarting the service.

The inventory agent carries higher mutation risk than the order exception agent: an incorrectly triggered inventory adjustment directly corrupts stock counts and can cause overselling. A fast, externally-operable kill switch is therefore a critical safety control.

## Decision

Implement a Redis-backed kill switch checked at the webhook entry point, before the idempotency check and before any background workflow is dispatched.

- **Redis key:** `agent:enabled:{store_domain}` — absent or `"1"` means enabled; `"0"` means disabled
- **Default when key is absent:** enabled (safe default — new stores are not inadvertently blocked)
- **Admin API:** `POST /api/admin/agent-control` (enable/disable), `GET /api/admin/agent-status` — authenticated via `X-Admin-Key` header
- **Response when suppressed:** HTTP 200 with `{"status": "accepted", "action": "suppressed_kill_switch"}` — Shopify does not retry 200 responses

The kill switch is checked **before** the idempotency guard so that suppressed events are not marked as "seen" — they can be replayed after re-enabling.

## Alternatives Considered

| Option | Rejected because |
|--------|-----------------|
| Environment variable / restart | Requires redeploy; too slow when a mutation wave is in flight |
| Approval_granted=False sentinel | Only blocks one workflow at a time; new webhooks still dispatch |
| Feature flag service | Added dependency; overkill for a binary on/off with existing Redis |
| Circuit breaker at Shopify client level | Only prevents the API call, not the LLM investigation spend |

## Consequences

- **Positive:** Halts all new mutation workflows in <1 second without touching the running process
- **Positive:** Per-store granularity — safe for future multi-tenant deployment
- **Positive:** Suppressed events remain replayable (not marked as idempotency-seen)
- **Negative:** Kill switch only affects new webhooks; in-flight LangGraph workflows awaiting human approval are not cancelled by the switch
- **Negative:** Redis availability is a soft prerequisite; if Redis is unreachable the switch defaults to enabled (tolerable — Redis outage is a separate incident)
