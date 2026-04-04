"""Slack interactive action handler.

Receives button callbacks from the Block Kit approval message posted by
the NotificationWorker. Verifies the request signature, parses the action,
and calls resume_workflow to continue the paused LangGraph workflow.

Slack sends the action payload as URL-encoded form data in a `payload` field.
"""
import asyncio
import hashlib
import hmac
import json
import time

import structlog
from fastapi import APIRouter, Form, HTTPException, Request, Response

from app.agent.graph import resume_workflow
from app.config import get_settings

logger = structlog.get_logger()
router = APIRouter()


def _verify_slack_signature(
    body: bytes, timestamp: str, signature: str, signing_secret: str
) -> bool:
    """Verify the Slack request signature to confirm the request came from Slack."""
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    base_string = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(),
        base_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/api/slack/actions")
async def handle_slack_action(request: Request, payload: str = Form(...)) -> Response:
    """Receive Slack interactive component payloads (button clicks).

    Slack expects a 200 response within 3 seconds; the actual workflow
    resumption is launched as a background task.
    """
    settings = get_settings()

    # Verify Slack signing secret when configured
    if settings.slack_signing_secret:
        body = await request.body()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not _verify_slack_signature(body, timestamp, signature, settings.slack_signing_secret):
            logger.warning("slack_action_invalid_signature")
            raise HTTPException(status_code=403, detail="Invalid Slack signature")

    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid payload JSON")

    # Ignore non-action events (e.g. shortcut, view_submission)
    if data.get("type") != "block_actions":
        return Response(status_code=200)

    actions = data.get("actions", [])
    if not actions:
        return Response(status_code=200)

    action = actions[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")

    # value format: "run_id:{run_id}"
    if not value.startswith("run_id:"):
        logger.warning("slack_action_unrecognized_value", value=value, action_id=action_id)
        return Response(status_code=200)

    run_id = value[len("run_id:"):]
    approved = action_id == "approve_discrepancy"

    user_info = data.get("user", {})
    reviewer_id = user_info.get("name") or user_info.get("id") or "slack_user"

    logger.info(
        "slack_approval_received",
        run_id=run_id,
        approved=approved,
        reviewer_id=reviewer_id,
    )

    # Resume workflow in the background — return 200 immediately so Slack doesn't retry
    asyncio.create_task(_resume_and_log(run_id=run_id, approved=approved, reviewer_id=reviewer_id))

    decision_text = "Approved" if approved else "Rejected"
    return Response(
        content=json.dumps({
            "response_type": "ephemeral",
            "text": f"Decision recorded: *{decision_text}* for run `{run_id}`.",
        }),
        media_type="application/json",
    )


async def _resume_and_log(run_id: str, approved: bool, reviewer_id: str) -> None:
    try:
        await resume_workflow(run_id=run_id, approved=approved, reviewer_id=reviewer_id)
        logger.info("slack_action_workflow_resumed", run_id=run_id, approved=approved)
    except Exception as exc:
        logger.error("slack_action_resume_failed", run_id=run_id, error=str(exc))
