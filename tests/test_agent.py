"""Integration-style tests for the full discrepancy agent workflow."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from tests.conftest import _make_state


@pytest.fixture(autouse=True)
def _init_graph_with_memory_saver():
    """Ensure the graph is initialized with MemorySaver for all tests in this module."""
    from app.agent.graph import init_graph
    init_graph(MemorySaver())


@pytest.mark.asyncio
async def test_detect_severity_all_thresholds():
    """Verify all four severity bands are classified correctly."""
    from app.agent.nodes import detect_discrepancy

    cases = [
        (100, 98, "minor"),     # 2%
        (100, 90, "moderate"),  # 10%
        (100, 75, "major"),     # 25%
        (100, 40, "critical"),  # 60%
    ]
    for expected, actual, expected_severity in cases:
        state = _make_state(expected_quantity=expected, actual_quantity=actual)
        result = await detect_discrepancy(state)
        assert result["severity"] == expected_severity, (
            f"expected={expected}, actual={actual} → {result['severity']} != {expected_severity}"
        )


@pytest.mark.asyncio
async def test_investigate_node_populates_root_cause():
    """investigate node should populate root_cause_analysis (mocked LLM)."""
    from app.agent import nodes, tools

    mock_shopify = AsyncMock()
    mock_shopify.get_inventory_levels = AsyncMock(return_value=[])
    mock_shopify.get_unfulfilled_orders_for_sku = AsyncMock(return_value=[])
    tools._shopify_client = mock_shopify

    with patch("app.agent.nodes._get_llm") as mock_llm_factory:
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "Root cause: likely data sync lag between ERP and Shopify."
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_factory.return_value = mock_llm

        state = _make_state(
            discrepancy_pct=20.0,
            severity="major",
        )
        result = await nodes.investigate(state)

    assert result["root_cause_analysis"] is not None
    assert len(result["root_cause_analysis"]) > 0


@pytest.mark.asyncio
async def test_full_workflow_approved_path():
    """Full workflow: detect → investigate → propose → (approve) → apply → notify → audit."""
    from app.agent import tools

    # Wire up mocks
    mock_shopify = AsyncMock()
    mock_shopify.get_inventory_levels = AsyncMock(return_value=[])
    mock_shopify.get_unfulfilled_orders_for_sku = AsyncMock(return_value=[])
    mock_shopify.set_inventory_quantity = AsyncMock(
        return_value={"inventoryAdjustmentGroup": {"reason": "correction"}}
    )
    tools._shopify_client = mock_shopify

    mock_slack = AsyncMock()
    mock_slack.post_inventory_alert = AsyncMock(return_value=True)
    tools._slack_client = mock_slack

    mock_sheets = AsyncMock()
    mock_sheets.find_row_by_run_id = AsyncMock(return_value=None)
    mock_sheets.append_row = AsyncMock(return_value={"updates": {"updatedRange": "A10"}})
    mock_sheets._spreadsheet_id = "test-sheet"
    tools._sheets_client = mock_sheets

    mock_db = AsyncMock()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    mock_session.refresh = AsyncMock()
    mock_db.return_value = mock_session
    tools._db_factory = mock_db

    mock_idempotency = AsyncMock()
    mock_idempotency.check_and_set = AsyncMock(return_value=True)
    mock_idempotency.delete_workflow_state = AsyncMock()
    tools._idempotency_service = mock_idempotency

    with patch("app.agent.nodes._get_llm") as mock_llm_factory:
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "Likely sync lag from recent bulk import."
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_factory.return_value = mock_llm

        from app.agent.graph import start_workflow, resume_workflow

        initial = _make_state(
            run_id="test-full-001",
            expected_quantity=100,
            actual_quantity=75,
        )

        # Phase 1: start workflow (runs until interrupt)
        run_id, proposal = await start_workflow(initial)
        assert run_id == "test-full-001"
        assert proposal.proposed_action in ("hold_for_review", "adjust_to_expected", "adjust_to_erp")

        # Phase 2: resume with approval
        final = await resume_workflow(
            run_id=run_id, approved=True, reviewer_id="ops-user", notes="looks correct"
        )
        assert final["approval_granted"] is True
