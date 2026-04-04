# ADR 006 — Redis Pub/Sub Event Router for Slack Notifications

**Status:** Accepted  
**Date:** 2026-04-03

## Context

The `post_slack_notification` tool was in `ALL_TOOLS`. Although the `notify` node is deterministic (not LLM-driven), having Slack in the tool list creates a risk surface: any future refactor that routes tools through the LLM would expose Slack credentials and delivery semantics to the model. More concretely, the clawhip architecture (claw-code post, April 2026) identifies this pattern as a structural smell — notifications are side-effects, not agent decisions.

Additionally, the `post_slack_notification` tool passed `redis_client` to `SlackClient.post_inventory_alert()` for deduplication. This meant tools.py held a reference to the raw Redis client, mixing infrastructure concerns into the tool layer.

## Decision

Implement a Redis pub/sub event router with two event types:

- `inventory_notification` — resolved discrepancy alert posted by the `notify` node.
- `approval_request` — interactive approval message posted by the `propose_resolution` node.

The `EventRouter.emit()` is injected into `nodes.py` via `inject_event_router()`. The `NotificationWorker` asyncio task subscribes to `shopify:events:inventory-notifications` and dispatches:
- `inventory_notification` → `SlackClient.post_inventory_alert()`
- `approval_request` → `SlackClient.post_interactive_approval()`

`post_slack_notification` is removed from `ALL_TOOLS` and `tools.py`. `_slack_client` and `_redis_client` are removed from `tools.py`'s injected dependencies.

## Consequences

**Positive:**
- Slack is entirely outside agent tool scope and LLM context.
- The interactive approval message (approval_request) is emitted from `propose_resolution` before the interrupt, so Slack messages arrive promptly without blocking the graph.
- `ALL_TOOLS` is reduced from 8 to 7 tools.
- Slack deduplication is no longer needed per-run since each run_id is unique; the old Redis dedup key is dropped.

**Negative:**
- Same trade-off as ADR 005 (order exception agent): fire-and-forget means lost events if the worker crashes between emit and delivery.
- The `approval_request` event triggers an interactive Slack message. If the worker is down at proposal time, operators won't receive the Slack prompt and must fall back to the REST approval endpoint.
