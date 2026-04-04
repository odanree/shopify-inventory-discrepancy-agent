# ADR 007 — Slack Interactive Message Approvals

**Status:** Accepted  
**Date:** 2026-04-03

## Context

The human-in-the-loop approval flow originally required operators to call a REST endpoint:
```
POST /api/approvals/{run_id}
{"approved": true, "reviewer_id": "ops-user", "notes": "..."}
```

This UX has two problems:
1. Operators must know the run_id (either from logs or a separate monitoring dashboard).
2. The action requires a terminal or API client — not practical for on-call ops.

After the `propose_resolution` node runs, the agent interrupts and waits indefinitely. Operators need a prompt that surfaces in their existing workflow (Slack is the ops communication channel).

## Decision

Extend the event router's `approval_request` event to trigger a Slack Block Kit interactive message with Approve and Reject buttons:

1. `propose_resolution` emits an `approval_request` event with the full proposal context.
2. `NotificationWorker` calls `SlackClient.post_interactive_approval()`, posting a Block Kit message with Approve/Reject buttons. The button `value` field encodes `run_id:{run_id}`.
3. When an operator clicks a button, Slack POSTs to `POST /api/slack/actions` (new `slack_actions.py` router).
4. The router verifies the Slack signing secret (`SLACK_SIGNING_SECRET`), parses the `action_id` and `run_id`, and calls `resume_workflow()` as an asyncio background task.
5. A 200 response with an ephemeral acknowledgment is returned to Slack immediately (within 3 seconds as required by Slack's API).

The REST approval endpoint (`/api/approvals/{run_id}`) is retained as a fallback for programmatic use and when the notification worker is unavailable.

## Requirements

- `SLACK_SIGNING_SECRET` must be set (from the Slack App config, under Interactivity settings).
- The Slack App's "Interactivity & Shortcuts" Request URL must be set to `https://{your-domain}/api/slack/actions`.
- The incoming webhook must belong to a Slack App (not a legacy webhook) for Block Kit actions to work.
- In dev, use `SLACK_SIGNING_SECRET=""` to skip signature verification (the check is skipped when the secret is empty).

## Consequences

**Positive:**
- Operators approve/reject directly from the Slack message — no terminal access needed.
- The approval message includes the full proposal context (SKU, discrepancy, root cause) so operators have the information to decide.
- The existing REST endpoint remains available as a fallback.

**Negative:**
- Requires a Slack App (not a legacy webhook) with interactivity enabled.
- Requires a publicly accessible URL for the Slack callback — ngrok or a deployed environment.
- The interactive message is not updated after the button click (Slack's `response_url` update requires an additional API call that is not implemented here).
- Replay attacks are mitigated by checking the `X-Slack-Request-Timestamp` is within 5 minutes, but an attacker with a valid timestamp+signature within that window could trigger approvals.
