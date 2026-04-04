# ADR 014 — Automated Weekly Impact Report

**Status:** Accepted  
**Date:** 2026-04-03

## Context

Same retention motivation as order exception agent ADR 013. For the inventory agent, the value proposition is more nuanced: the client needs to see not just that discrepancies were detected but that the agent's proposals were good enough to be approved and that inventory accuracy improved as a result.

## Decision

Add a `weekly_report` asyncio scheduler task delivering a Slack Block Kit message every Monday via the existing `SlackClient.post_inventory_alert()`:

**Report fields:**
- Period (date range)
- Discrepancies Detected
- Resolutions Approved (count + approval rate %)
- Inventory Transfers (cross-location moves)
- Pending Review (still awaiting operator decision)
- Avg Discrepancy % (magnitude of variance)

Redis key `agent:weekly_report:inventory:last_sent` (25h TTL) prevents duplicates on restart.

**Block Kit delivery** via `post_inventory_alert` preserves the same visual styling as operational alerts — consistent with the dashboard's Block Kit fields format.

## Consequences

- **Positive:** Clients see approval rate as a trust signal — a high approval rate validates that the agent's proposals are accurate
- **Positive:** "Inventory Transfers" metric differentiates the multi-location transfer capability from a basic adjustment tool
- **Positive:** "Pending Review" count creates a natural action prompt if the client has a backlog
- **Negative:** Approval rate is computed over all 7-day events including auto-resolved ones; edge cases (mass-rejected batch) can make the number misleading without additional context
