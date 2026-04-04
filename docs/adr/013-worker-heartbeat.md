# ADR 013 — Notification Worker Heartbeat

**Status:** Accepted  
**Date:** 2026-04-03

## Context

Same problem as order exception agent ADR 012, with higher stakes: if the inventory `NotificationWorker` stalls, `approval_request` events published to Redis pub/sub are silently dropped. Operators never receive the Slack interactive message prompting them to approve/reject an inventory mutation. The LangGraph workflow sits at the interrupt node waiting indefinitely for an approval that will never come. The client's Shopify inventory remains uncorrected.

## Decision

Add a heartbeat loop to `NotificationWorker` following the same pattern as the order exception agent:

- `_heartbeat_loop()` runs concurrently with `_run_subscribe_with_retry()` via `asyncio.gather`
- Pulses `agent:heartbeat:inventory-discrepancy-worker` Redis key every **5 minutes** with **700s TTL**
- `GET /health` checks the key and reports `worker: ok/stale/not_started`
- Sentry `capture_message` at `critical` level when health is degraded
- HTTP 503 returned when any check is not `"ok"`

## Consequences

Same as order exception ADR 012. Additional consequence for inventory agent: a stale worker heartbeat is a direct signal that pending approval workflows will not progress, allowing an operator to investigate before the 24-hour approval expiry (`APPROVAL_EXPIRY_HOURS`) causes them to time out.
