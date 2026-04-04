from typing import Literal, Optional

from pydantic import BaseModel, Field


class DiscrepancyEvent(BaseModel):
    sku: str
    inventory_item_id: str
    location_id: str
    expected_quantity: int
    actual_quantity: int
    reported_by: str = "system"
    source: Literal["manual", "webhook", "scheduled"] = "manual"


class ApprovalRequest(BaseModel):
    approved: bool
    reviewer_id: str
    notes: Optional[str] = None


class ResolutionProposal(BaseModel):
    run_id: str
    sku: str
    inventory_item_id: str
    location_id: str
    discrepancy_pct: float
    severity: str
    root_cause_analysis: str
    proposed_action: str
    proposed_quantity: int
    affected_orders: list[str] = Field(default_factory=list)
    estimated_impact: str = ""
