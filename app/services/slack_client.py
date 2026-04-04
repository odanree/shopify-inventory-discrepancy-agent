import httpx
import structlog

logger = structlog.get_logger()

SEVERITY_COLORS = {
    "info": "#0070D2",
    "warning": "#FF7043",
    "critical": "#D32F2F",
}


class SlackClient:
    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=10.0)

    async def post_inventory_alert(
        self,
        channel: str,
        title: str,
        fields: dict[str, str],
        severity: str,
        run_id: str,
        redis_client=None,
    ) -> bool:
        """Post a Block Kit inventory alert. Idempotent via Redis dedup on run_id."""
        if not self._webhook_url:
            logger.warning("slack_webhook_url_not_configured")
            return False

        # Idempotency: skip if already sent for this run_id
        if redis_client is not None:
            dedup_key = f"slack:sent:{run_id}"
            already_sent = not await redis_client.set(dedup_key, "1", nx=True, ex=3600)
            if already_sent:
                logger.info("slack_alert_deduped", run_id=run_id)
                return True

        color = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["info"])
        field_blocks = [
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{k}*\n{v}"}
                    for k, v in list(fields.items())[:10]
                ],
            }
        ]

        payload = {
            "channel": channel,
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "header",
                            "text": {"type": "plain_text", "text": f"[{severity.upper()}] {title}"},
                        },
                        *field_blocks,
                        {
                            "type": "context",
                            "elements": [
                                {"type": "mrkdwn", "text": f"Run ID: `{run_id}` | Severity: *{severity}*"}
                            ],
                        },
                    ],
                }
            ],
        }

        try:
            resp = await self._client.post(self._webhook_url, json=payload)
            resp.raise_for_status()
            logger.info("slack_inventory_alert_sent", run_id=run_id, severity=severity)
            return True
        except Exception as exc:
            logger.error("slack_alert_failed", run_id=run_id, error=str(exc))
            return False

    async def post_interactive_approval(
        self,
        channel: str,
        run_id: str,
        sku: str,
        discrepancy_pct: float,
        severity: str,
        proposed_action: str,
        proposed_quantity: int | None,
        expected_quantity: int,
        open_orders_count: int,
        root_cause_analysis: str,
    ) -> bool:
        """Post a Slack Block Kit message with Approve/Reject buttons.

        Requires the Slack incoming webhook to belong to a Slack App with
        interactivity enabled and a request URL pointing to /api/slack/actions.
        """
        if not self._webhook_url:
            logger.warning("slack_webhook_url_not_configured")
            return False

        color = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["info"])
        action_label = proposed_action.replace("_", " ").title()
        fields = [
            {"type": "mrkdwn", "text": f"*SKU*\n{sku}"},
            {"type": "mrkdwn", "text": f"*Discrepancy*\n{discrepancy_pct}% ({severity})"},
            {"type": "mrkdwn", "text": f"*Proposed Action*\n{action_label}"},
            {"type": "mrkdwn", "text": f"*Proposed Qty*\n{proposed_quantity or expected_quantity}"},
            {"type": "mrkdwn", "text": f"*Open Orders*\n{open_orders_count}"},
            {"type": "mrkdwn", "text": f"*Run ID*\n`{run_id}`"},
        ]

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"[{severity.upper()}] Inventory Approval Required — {sku}",
                },
            },
            {"type": "section", "fields": fields},
        ]

        if root_cause_analysis:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root Cause*\n{root_cause_analysis[:300]}",
                },
            })

        blocks.append({
            "type": "actions",
            "block_id": "approval_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve_discrepancy",
                    "value": f"run_id:{run_id}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "reject_discrepancy",
                    "value": f"run_id:{run_id}",
                },
            ],
        })

        payload = {
            "channel": channel,
            "attachments": [{"color": color, "blocks": blocks}],
        }

        try:
            resp = await self._client.post(self._webhook_url, json=payload)
            resp.raise_for_status()
            logger.info("slack_approval_message_sent", run_id=run_id, severity=severity)
            return True
        except Exception as exc:
            logger.error("slack_approval_message_failed", run_id=run_id, error=str(exc))
            return False

    async def close(self):
        await self._client.aclose()
