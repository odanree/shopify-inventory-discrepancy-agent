from typing import Annotated, Any

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class DiscrepancyState(TypedDict):
    run_id: str
    sku: str
    inventory_item_id: str
    location_id: str
    expected_quantity: int
    actual_quantity: int
    discrepancy_pct: float
    severity: str | None  # minor | moderate | major | critical

    # Set by investigate node
    recent_adjustments: list[dict[str, Any]] | None
    open_orders: list[dict[str, Any]] | None
    open_orders_count: int | None
    root_cause_analysis: str | None

    # Set by propose node
    proposed_action: str | None  # hold_for_review | adjust_to_expected | adjust_to_erp | request_3pl_audit | rejected
    proposed_quantity: int | None

    # Set by approval endpoint via aupdate_state
    approval_granted: bool | None  # None = pending, True = approved, False = rejected
    approved_by: str | None
    approval_notes: str | None

    # Set by apply_mutation node
    mutation_applied: bool
    mutation_result: dict[str, Any] | None

    # Set by verify_mutation node
    verification_passed: bool | None
    retry_count: int

    # Set by notify node
    slack_notified: bool
    sheets_row: str | None

    # Metadata
    tool_calls_log: list[dict[str, Any]]
    error: str | None
    messages: Annotated[list, add_messages]
