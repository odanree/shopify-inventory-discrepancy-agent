# ADR 015 — LLM Token Usage and Cost Tracking

**Status:** Accepted  
**Date:** 2026-04-08

## Context

The `investigate` node invokes Claude (`claude-sonnet-4-6`) on every discrepancy workflow to
generate a root cause analysis. The investigate call is more expensive than the order exception
agent's triage call: the prompt includes inventory levels, recent adjustments, open orders, and
multi-location data — typically 400–700 input tokens vs. ~200 for a single-field classifier.

Token consumption and API cost were invisible in the dashboard. As workflow volume scales
(webhook-driven or scheduled reconciliation), understanding cost-per-event matters for:

- Budget forecasting (investigate is a heavier LLM call than a simple classifier)
- Detecting prompt drift (input token growth = context bloat in investigation data)
- Demonstrating operational efficiency of the automated triage loop

## Decision

Capture `response.usage_metadata` from the LangChain `AIMessage` in `investigate` and propagate
it through state to the audit record.

**Data flow:**
```
investigate → state (llm_input_tokens, llm_output_tokens)
           → audit node → write_audit_record tool
           → DiscrepancyAuditLog (input_tokens, output_tokens, cost_usd columns)
           → /api/dashboard/stats
           → Dashboard cards
```

**Schema changes:** Three nullable columns on `discrepancy_audit_logs`:
- `input_tokens INTEGER`
- `output_tokens INTEGER`
- `cost_usd DOUBLE PRECISION` — computed at write time

**Cost formula** (claude-sonnet-4-6):
```
cost_usd = (input_tokens × $3.00 + output_tokens × $15.00) / 1,000,000
```

**Migration strategy:** Alembic migration `002_token_columns` uses
`ADD COLUMN IF NOT EXISTS` — idempotent and startup-safe.

**Dashboard additions:**
- **LLM Cost (7d)** card — total with avg cost/event sub-label
- **Tokens Used (7d)** card — combined total with in/out breakdown

## Alternatives Considered

| Option | Rejected because |
|--------|-----------------|
| Store in `metadata` JSONB only | Can't aggregate with SQL `SUM`/`AVG` |
| LangFuse for cost tracking | Optional/disabled in production; adds external dependency |
| Alembic-less inline ALTER TABLE | Not idempotent via code path; replaced by migration 002 |

## Consequences

- **Positive:** Cost visibility with no external service dependency
- **Positive:** Input token growth is a leading indicator of prompt bloat — investigate prompt
  aggregates more context than triage, so monitoring is more critical here
- **Positive:** Migration is idempotent; historical rows are `NULL` (accurate, not misleading)
- **Negative:** Only the investigate call is instrumented; if additional LLM nodes are added,
  they must each capture usage separately and accumulate into state
- **Negative:** Pricing hardcoded at write time — rate changes affect future rows only
