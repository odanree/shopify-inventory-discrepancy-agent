"""Operations dashboard for the inventory discrepancy agent.

GET /dashboard           — serves a single-page HTML dashboard (Basic Auth protected)
GET /api/dashboard/stats — returns JSON stats (consumed by the HTML page)
"""
import secrets
from datetime import datetime, timedelta, timezone
from textwrap import dedent
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select

import app.db.session as _db_session
from app.models.db import DiscrepancyAuditLog

logger = structlog.get_logger()
router = APIRouter(tags=["dashboard"])

_security = HTTPBasic(auto_error=False)


def _require_dashboard_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(_security),
) -> None:
    """HTTP Basic Auth guard. Skipped when admin_api_key is empty (dev/test)."""
    from app.config import get_settings
    key = get_settings().admin_api_key
    if not key:
        return
    if credentials is None or not secrets.compare_digest(
        credentials.password.encode(), key.encode()
    ):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Inventory Discrepancy Agent"'},
            detail="Unauthorized",
        )

_SEVEN_DAYS = timedelta(days=7)


@router.get("/api/dashboard/stats")
async def get_stats(request: Request):
    from app.config import get_settings
    settings = get_settings()
    seven_days_ago = datetime.now(timezone.utc) - _SEVEN_DAYS

    async with _db_session.AsyncSessionLocal() as session:
        total_7d = await session.scalar(
            select(func.count(DiscrepancyAuditLog.id)).where(
                DiscrepancyAuditLog.created_at >= seven_days_ago
            )
        )
        pending_count = await session.scalar(
            select(func.count(DiscrepancyAuditLog.id)).where(
                DiscrepancyAuditLog.approved.is_(None),
                DiscrepancyAuditLog.proposed_action.isnot(None),
            )
        )
        approved_count = await session.scalar(
            select(func.count(DiscrepancyAuditLog.id)).where(
                DiscrepancyAuditLog.approved.is_(True),
                DiscrepancyAuditLog.created_at >= seven_days_ago,
            )
        )
        avg_discrepancy = await session.scalar(
            select(func.avg(DiscrepancyAuditLog.discrepancy_pct)).where(
                DiscrepancyAuditLog.created_at >= seven_days_ago
            )
        )
        by_action_rows = (
            await session.execute(
                select(DiscrepancyAuditLog.proposed_action, func.count(DiscrepancyAuditLog.id))
                .where(DiscrepancyAuditLog.created_at >= seven_days_ago)
                .group_by(DiscrepancyAuditLog.proposed_action)
                .order_by(func.count(DiscrepancyAuditLog.id).desc())
            )
        ).all()

        token_row = (
            await session.execute(
                select(
                    func.sum(DiscrepancyAuditLog.input_tokens),
                    func.sum(DiscrepancyAuditLog.output_tokens),
                    func.sum(DiscrepancyAuditLog.cost_usd),
                ).where(DiscrepancyAuditLog.created_at >= seven_days_ago)
            )
        ).one()

        # Pending approvals details
        pending_rows = (
            await session.execute(
                select(
                    DiscrepancyAuditLog.run_id,
                    DiscrepancyAuditLog.sku,
                    DiscrepancyAuditLog.discrepancy_pct,
                    DiscrepancyAuditLog.proposed_action,
                    DiscrepancyAuditLog.expected_qty,
                    DiscrepancyAuditLog.actual_qty,
                    DiscrepancyAuditLog.created_at,
                )
                .where(
                    DiscrepancyAuditLog.approved.is_(None),
                    DiscrepancyAuditLog.proposed_action.isnot(None),
                )
                .order_by(DiscrepancyAuditLog.created_at.asc())
                .limit(50)
            )
        ).all()

    total = total_7d or 0
    approval_rate = round((approved_count or 0) / max(total, 1) * 100, 1)

    total_input_tokens = int(token_row[0] or 0)
    total_output_tokens = int(token_row[1] or 0)
    total_cost_usd = float(token_row[2] or 0.0)
    avg_cost_per_event = round(total_cost_usd / max(total, 1), 6)

    return {
        "window": "7d",
        "shadow_mode": settings.agent_mode == "shadow",
        "total_processed": total,
        "pending_approvals": pending_count or 0,
        "approval_rate_pct": approval_rate,
        "avg_discrepancy_pct": round(avg_discrepancy or 0, 1),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": round(total_cost_usd, 6),
        "avg_cost_per_event_usd": avg_cost_per_event,
        "by_action": [
            {"action": r[0] or "unknown", "count": r[1]} for r in by_action_rows
        ],
        "pending_items": [
            {
                "run_id": r[0],
                "sku": r[1],
                "discrepancy_pct": round(r[2], 1),
                "proposed_action": r[3],
                "expected_qty": r[4],
                "actual_qty": r[5],
                "created_at": r[6].isoformat() if r[6] else None,
            }
            for r in pending_rows
        ],
    }


_DASHBOARD_HTML = dedent("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Inventory Discrepancy Agent — Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  header { background: #1e293b; padding: 1.25rem 2rem;
           border-bottom: 1px solid #334155; display: flex;
           align-items: center; justify-content: space-between; }
  header h1 { font-size: 1.1rem; font-weight: 600; color: #f1f5f9; }
  header span { font-size: 0.75rem; color: #64748b; }
  .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
           gap: 1rem; margin-bottom: 2rem; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 10px;
          padding: 1.25rem; }
  .card .label { font-size: 0.75rem; color: #94a3b8; text-transform: uppercase;
                  letter-spacing: .05em; margin-bottom: .5rem; }
  .card .value { font-size: 2rem; font-weight: 700; color: #f8fafc; }
  .card .sub { font-size: 0.75rem; color: #64748b; margin-top: .25rem; }
  .card.green .value { color: #4ade80; }
  .card.yellow .value { color: #facc15; }
  .card.red .value { color: #f87171; }
  .card.blue .value { color: #60a5fa; }
  h2 { font-size: 0.875rem; font-weight: 600; color: #94a3b8;
       text-transform: uppercase; letter-spacing: .05em; margin-bottom: 1rem; }
  .section { margin-bottom: 2rem; }
  table { width: 100%; border-collapse: collapse; background: #1e293b;
          border-radius: 10px; overflow: hidden;
          border: 1px solid #334155; }
  th, td { padding: .75rem 1rem; text-align: left; font-size: .875rem; }
  th { color: #94a3b8; font-weight: 500; border-bottom: 1px solid #334155; }
  td { color: #e2e8f0; border-bottom: 1px solid #1e293b; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: .2rem .6rem; border-radius: 999px;
           font-size: .7rem; font-weight: 600; }
  .badge.adjust { background: #1e3a5f; color: #93c5fd; }
  .badge.investigate { background: #713f12; color: #fde68a; }
  .badge.escalate { background: #7f1d1d; color: #fca5a5; }
  .badge.auto_resolve { background: #14532d; color: #86efac; }
  .badge.transfer { background: #4a1d96; color: #c4b5fd; }
  .badge.unknown { background: #1e293b; color: #94a3b8; }
  .btn { display: inline-block; padding: .3rem .8rem; border-radius: 6px;
         font-size: .75rem; font-weight: 600; cursor: pointer; border: none;
         transition: opacity .15s; }
  .btn:hover { opacity: .85; }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn-approve { background: #166534; color: #86efac; }
  .btn-reject { background: #7f1d1d; color: #fca5a5; margin-left: .4rem; }
  .refresh { font-size: .7rem; color: #475569; }
  .empty { color: #475569; font-size: .875rem; }
  .pct-high { color: #f87171; }
  .pct-med { color: #facc15; }
  .pct-low { color: #4ade80; }
  .shadow-banner { background: #78350f; border-bottom: 2px solid #d97706;
                   padding: .6rem 2rem; font-size: .8rem; font-weight: 600;
                   color: #fde68a; letter-spacing: .03em; display: none; }
</style>
</head>
<body>
<div class="shadow-banner" id="shadow-banner">
  ⚠ SHADOW MODE — mutations are logged but not applied to Shopify
</div>
<header>
  <h1>📦 Inventory Discrepancy Agent</h1>
  <span class="refresh" id="last-updated">Loading…</span>
</header>
<div class="container">
  <div class="cards" id="cards">
    <div class="card"><div class="label">Loading…</div><div class="value">—</div></div>
  </div>
  <div class="section">
    <h2>Pending Approvals</h2>
    <table><thead><tr>
      <th>SKU</th><th>Discrepancy</th><th>Expected</th><th>Actual</th>
      <th>Proposed Action</th><th>Queued</th><th>Actions</th>
    </tr></thead>
    <tbody id="pending-rows"><tr><td colspan="7">Loading…</td></tr></tbody></table>
  </div>
  <div class="section">
    <h2>Action Breakdown — Last 7 Days</h2>
    <table><thead><tr><th>Action</th><th>Count</th></tr></thead>
    <tbody id="action-rows"><tr><td colspan="2">Loading…</td></tr></tbody></table>
  </div>
</div>
<script>
const ACTION_BADGE = {
  adjust_inventory:'adjust',
  investigate:'investigate',
  escalate_to_ops:'escalate',
  auto_resolve:'auto_resolve',
  transfer_inventory:'transfer',
};

function pctClass(pct) {
  if (pct >= 20) return 'pct-high';
  if (pct >= 10) return 'pct-med';
  return 'pct-low';
}

function relTime(iso) {
  if (!iso) return '—';
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}

async function act(runId, approved, btn) {
  btn.disabled = true;
  const row = btn.closest('tr');
  try {
    const r = await fetch('/api/approvals/' + runId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approved, reviewer_id: 'dashboard', notes: '' }),
    });
    if (!r.ok) throw new Error(await r.text());
    row.style.opacity = '.4';
    row.querySelectorAll('.btn').forEach(b => b.disabled = true);
    setTimeout(() => row.remove(), 1500);
  } catch(e) {
    btn.disabled = false;
    alert('Action failed: ' + e.message);
  }
}

async function load() {
  try {
    const r = await fetch('/api/dashboard/stats');
    const d = await r.json();
    document.getElementById('last-updated').textContent =
      'Last updated: ' + new Date().toLocaleTimeString();
    document.getElementById('shadow-banner').style.display = d.shadow_mode ? 'block' : 'none';

    const fmtTokens = n => n >= 1000 ? (n/1000).toFixed(1)+'k' : String(n);
    const fmtCost = n => n < 0.01 ? '$' + n.toFixed(4) : '$' + n.toFixed(3);
    const pendingColor = d.pending_approvals > 0 ? 'yellow' : 'green';
    document.getElementById('cards').innerHTML = `
      <div class="card blue">
        <div class="label">Events Processed (7d)</div>
        <div class="value">${d.total_processed}</div>
        <div class="sub">discrepancy events triaged</div>
      </div>
      <div class="card ${pendingColor}">
        <div class="label">Pending Approvals</div>
        <div class="value">${d.pending_approvals}</div>
        <div class="sub">awaiting operator decision</div>
      </div>
      <div class="card ${d.approval_rate_pct >= 80 ? 'green' : 'yellow'}">
        <div class="label">Approval Rate</div>
        <div class="value">${d.approval_rate_pct}%</div>
        <div class="sub">of proposals approved (7d)</div>
      </div>
      <div class="card ${d.avg_discrepancy_pct >= 15 ? 'red' : 'blue'}">
        <div class="label">Avg Discrepancy</div>
        <div class="value">${d.avg_discrepancy_pct}%</div>
        <div class="sub">mean variance (7d)</div>
      </div>
      <div class="card yellow">
        <div class="label">LLM Cost (7d)</div>
        <div class="value">${fmtCost(d.total_cost_usd)}</div>
        <div class="sub">${fmtCost(d.avg_cost_per_event_usd)} avg per event</div>
      </div>
      <div class="card">
        <div class="label">Tokens Used (7d)</div>
        <div class="value">${fmtTokens(d.total_input_tokens + d.total_output_tokens)}</div>
        <div class="sub">${fmtTokens(d.total_input_tokens)} in · ${fmtTokens(d.total_output_tokens)} out</div>
      </div>`;

    document.getElementById('pending-rows').innerHTML = d.pending_items.length
      ? d.pending_items.map(p => `<tr>
          <td><strong>${p.sku}</strong></td>
          <td class="${pctClass(p.discrepancy_pct)}">${p.discrepancy_pct}%</td>
          <td>${p.expected_qty}</td>
          <td>${p.actual_qty}</td>
          <td><span class="badge ${ACTION_BADGE[p.proposed_action]||'unknown'}">${p.proposed_action}</span></td>
          <td>${relTime(p.created_at)}</td>
          <td>
            <button class="btn btn-approve" onclick="act('${p.run_id}', true, this)">Approve</button>
            <button class="btn btn-reject" onclick="act('${p.run_id}', false, this)">Reject</button>
          </td></tr>`).join('')
      : '<tr><td colspan="7" class="empty">No pending approvals</td></tr>';

    document.getElementById('action-rows').innerHTML = d.by_action.length
      ? d.by_action.map(a => `<tr>
          <td><span class="badge ${ACTION_BADGE[a.action]||'unknown'}">${a.action}</span></td>
          <td>${a.count}</td></tr>`).join('')
      : '<tr><td colspan="2" class="empty">No data yet</td></tr>';
  } catch(e) {
    document.getElementById('cards').innerHTML =
      '<div class="card red"><div class="label">Error</div><div class="value">—</div>' +
      '<div class="sub">' + e.message + '</div></div>';
  }
}
load();
setInterval(load, 30000);
</script>
</body>
</html>
""")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(_: None = Depends(_require_dashboard_auth)):
    return HTMLResponse(content=_DASHBOARD_HTML)
