# ADR 012 — Sentry Error Monitoring

**Status:** Accepted  
**Date:** 2026-04-03

## Context

The inventory agent is the higher-stakes of the two agents: a failed or repeated inventory mutation has direct financial consequences for the client. When `inventorySetOnHandQuantities` fails silently, or a transfer mutation is retried incorrectly due to a Shopify API version change, the operator needs to know before the client notices a stock discrepancy in their Shopify admin.

structlog provides rich local output but requires active log monitoring. A push-based error alerting system is required for a client-facing deployment.

## Decision

Integrate Sentry SDK with FastAPI and SQLAlchemy integrations, using the same pattern as the order exception agent.

**Initialization:** In `lifespan()`, if `SENTRY_DSN` is set:
```python
sentry_sdk.init(
    dsn=settings.sentry_dsn,
    environment=settings.app_env,
    traces_sample_rate=0.2,
    integrations=[FastApiIntegration(), SqlalchemyIntegration()],
)
```

**Explicit captures:**
- `audit` node: `sentry_sdk.capture_message()` at `error` level when `state.error` is set and `mutation_applied is False` and `not shadowed` — captures the SKU, run_id, proposed_action, error, and retry_count as structured context

**Key difference from order exception agent:** The capture fires in the `audit` node rather than a `dead_letter` node, because the inventory agent has no dead-letter queue. Any unresolved mutation failure ends at `audit` with `mutation_applied=False`.

**Graceful degradation:** All Sentry calls are wrapped in `try/except ImportError`. If the package is not installed, the agent runs normally.

## Alternatives Considered

Same analysis as order exception ADR 011 — same decision, same rationale.

## Consequences

- **Positive:** Operators are alerted when mutations fail before clients notice inventory discrepancies
- **Positive:** Structured context (SKU, run_id, retry_count) allows operators to assess impact and manually correct if needed
- **Positive:** Zero behavioral change when DSN is not set
- **Negative:** `sentry-sdk[fastapi]` adds a production dependency
- **Negative:** `extras` dict may contain inventory SKUs and quantities — verify data residency compliance before enabling in production
