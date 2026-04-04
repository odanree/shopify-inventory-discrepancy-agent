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

    async def close(self):
        await self._client.aclose()
