"""Tests for the approval gate and apply_mutation safety guard."""
import pytest

from tests.conftest import _make_state


@pytest.mark.asyncio
async def test_apply_mutation_skipped_when_not_approved():
    """apply_mutation should skip the mutation when approval_granted is False."""
    from app.agent.nodes import apply_mutation

    state = _make_state(
        severity="major",
        proposed_action="adjust_to_expected",
        proposed_quantity=100,
        approval_granted=False,
        approved_by=None,
    )
    result = await apply_mutation(state)
    assert result["mutation_applied"] is False
    assert result["mutation_result"]["skipped"] is True


@pytest.mark.asyncio
async def test_apply_mutation_skipped_when_pending():
    """apply_mutation should skip when approval_granted is None (still pending)."""
    from app.agent.nodes import apply_mutation

    state = _make_state(
        proposed_action="adjust_to_expected",
        proposed_quantity=100,
        approval_granted=None,
    )
    result = await apply_mutation(state)
    assert result["mutation_applied"] is False


@pytest.mark.asyncio
async def test_adjust_inventory_tool_refuses_without_approval():
    """adjust_inventory_level tool must raise PermissionError if ctx var not set."""
    from app.agent.tools import adjust_inventory_level, _approval_granted_ctx
    import contextvars

    # Ensure context var is False
    _approval_granted_ctx.set(False)

    with pytest.raises(PermissionError, match="requires approval_granted"):
        await adjust_inventory_level.ainvoke(
            {
                "inventory_item_id": "inv-001",
                "location_id": "loc-001",
                "available_quantity": 100,
                "reason": "test",
            }
        )


@pytest.mark.asyncio
async def test_apply_mutation_with_approval_calls_shopify():
    """When approved=True, apply_mutation should call adjust_inventory_level."""
    from unittest.mock import AsyncMock, patch
    from app.agent import nodes, tools

    mock_shopify = AsyncMock()
    mock_shopify.set_inventory_quantity = AsyncMock(
        return_value={"inventoryAdjustmentGroup": {"reason": "correction"}}
    )
    tools._shopify_client = mock_shopify

    state = _make_state(
        severity="major",
        proposed_action="adjust_to_expected",
        proposed_quantity=100,
        expected_quantity=100,
        actual_quantity=75,
        discrepancy_pct=25.0,
        approval_granted=True,
        approved_by="ops-user-1",
    )

    result = await nodes.apply_mutation(state)
    assert result["mutation_applied"] is True
    mock_shopify.set_inventory_quantity.assert_called_once()
