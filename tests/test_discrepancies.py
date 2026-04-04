"""Tests for discrepancy detection and resolution proposal logic."""
import pytest

from tests.conftest import _make_state


@pytest.mark.asyncio
async def test_detect_discrepancy_calculates_pct():
    """detect_discrepancy should compute discrepancy_pct and classify severity."""
    from app.agent.nodes import detect_discrepancy

    state = _make_state(expected_quantity=100, actual_quantity=80)
    result = await detect_discrepancy(state)

    assert result["discrepancy_pct"] == 20.0
    assert result["severity"] == "major"


@pytest.mark.asyncio
async def test_detect_discrepancy_critical():
    from app.agent.nodes import detect_discrepancy

    state = _make_state(expected_quantity=100, actual_quantity=40)
    result = await detect_discrepancy(state)

    assert result["discrepancy_pct"] == 60.0
    assert result["severity"] == "critical"


@pytest.mark.asyncio
async def test_detect_discrepancy_minor():
    from app.agent.nodes import detect_discrepancy

    state = _make_state(expected_quantity=100, actual_quantity=97)
    result = await detect_discrepancy(state)

    assert result["discrepancy_pct"] == 3.0
    assert result["severity"] == "minor"


@pytest.mark.asyncio
async def test_propose_resolution_critical_always_holds():
    """Critical severity should always produce hold_for_review."""
    from app.agent.nodes import propose_resolution

    state = _make_state(
        discrepancy_pct=60.0,
        severity="critical",
        open_orders_count=0,
    )
    result = await propose_resolution(state)
    assert result["proposed_action"] == "hold_for_review"


@pytest.mark.asyncio
async def test_propose_resolution_major_with_many_orders_holds():
    """Major discrepancy with >5 open orders should hold."""
    from app.agent.nodes import propose_resolution

    state = _make_state(
        discrepancy_pct=25.0,
        severity="major",
        open_orders_count=10,
    )
    result = await propose_resolution(state)
    assert result["proposed_action"] == "hold_for_review"


@pytest.mark.asyncio
async def test_propose_resolution_major_no_orders_adjusts():
    """Major discrepancy with no open orders should adjust."""
    from app.agent.nodes import propose_resolution

    state = _make_state(
        discrepancy_pct=25.0,
        severity="major",
        open_orders_count=0,
    )
    result = await propose_resolution(state)
    assert result["proposed_action"] == "adjust_to_expected"


@pytest.mark.asyncio
async def test_propose_resolution_moderate_adjusts():
    from app.agent.nodes import propose_resolution

    state = _make_state(
        discrepancy_pct=10.0,
        severity="moderate",
        open_orders_count=2,
    )
    result = await propose_resolution(state)
    assert result["proposed_action"] == "adjust_to_expected"
