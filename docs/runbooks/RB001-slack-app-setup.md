# RB001 — Slack App Setup for Inventory Discrepancy Agent

**Last updated:** 2026-04-08  
**Area:** Slack integration / deployment

## Purpose

Step-by-step guide to configure the Slack app that powers:
- Approval Required alerts (Block Kit with Approve/Reject buttons)
- Resolution notifications (posted after the workflow completes)
- Button callbacks routed to `/api/slack/actions`

---

## Environment variables

See `docs/runbooks/env.md` (gitignored) for real values.

| Variable | Description |
|----------|-------------|
| `$PROD_HOST` | Production server hostname or IP |
| `$PROD_USER` | SSH user (e.g. `root`) |
| `$SERVICE_URL` | Public HTTPS URL of the agent |
| `$DB_USER` | PostgreSQL app user |
| `$DB_NAME` | PostgreSQL database name |
| `$INVENTORY_AGENT_CONTAINER` | Docker container name |
| `$POSTGRES_CONTAINER` | Docker postgres container name |

---

## Prerequisites

- Admin access to the Slack workspace
- The agent deployed and reachable at `$SERVICE_URL`
- Access to `portfolio-infra/.env` on the deployment server

---

## Step 1 — Create (or locate) the Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From scratch**
3. Name: `Inventory Discrepancy Agent` | Workspace: your workspace
4. Click **Create App**

If an app already exists for this workspace, select it instead.

---

## Step 2 — Add an Incoming Webhook

Incoming webhooks are how the agent posts messages. Each webhook is scoped to one channel.

1. In the app sidebar → **Incoming Webhooks** → toggle **Activate Incoming Webhooks** to On
2. Click **Add New Webhook to Workspace**
3. Select channel: `#inventory-alerts` (or create it first)
4. Click **Allow**
5. Copy the webhook URL — it looks like:
   ```
   https://hooks.slack.com/services/T.../B.../...
   ```
6. Add to `portfolio-infra/.env`:
   ```
   SHOPIFY_INVENTORY_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   ```

---

## Step 3 — Enable Interactivity

Interactivity allows Slack to POST button-click events back to the agent.

1. In the app sidebar → **Interactivity & Shortcuts**
2. Toggle **Interactivity** to On
3. Set **Request URL** to:
   ```
   $SERVICE_URL/api/slack/actions
   ```
4. Click **Save Changes**

Slack will verify the URL is reachable. If verification fails, check that the agent is
running and the reverse proxy is correctly routing the domain.

---

## Step 4 — Get the Signing Secret

The signing secret lets the agent verify that button callbacks came from Slack (not a
spoofed request).

1. In the app sidebar → **Basic Information** → **App Credentials**
2. Copy **Signing Secret**
3. Add to `portfolio-infra/.env`:
   ```
   SHOPIFY_INVENTORY_SLACK_SIGNING_SECRET=<signing-secret>
   ```

---

## Step 5 — Wire into docker-compose.yml

Ensure `shopify-inventory-agent` service in `docker-compose.yml` has:

```yaml
environment:
  SLACK_WEBHOOK_URL: ${SHOPIFY_INVENTORY_SLACK_WEBHOOK_URL}
  SLACK_SIGNING_SECRET: ${SHOPIFY_INVENTORY_SLACK_SIGNING_SECRET}
```

---

## Step 6 — Deploy

```bash
# Sync .env to server
scp portfolio-infra/.env $PROD_USER@$PROD_HOST:/opt/portfolio-infra/.env

# Restart container (no rebuild needed — env-only change)
ssh $PROD_USER@$PROD_HOST "cd /opt/portfolio-infra && docker compose up -d --no-build shopify-inventory-agent"

# Verify env vars are set
ssh $PROD_USER@$PROD_HOST "docker exec $INVENTORY_AGENT_CONTAINER env | grep SLACK"
```

---

## Step 7 — Verify end-to-end

```bash
# Trigger a discrepancy
python scripts/seed_demo.py --url $SERVICE_URL --scenario minor

# Wait ~5s, check Slack for the approval message with Approve/Reject buttons
# Click Approve in Slack
# Verify resolution message appears in Slack
# Verify audit record in DB
ssh $PROD_USER@$PROD_HOST "docker exec $POSTGRES_CONTAINER psql -U $DB_USER -d $DB_NAME \
  -c 'SELECT sku, proposed_action, approved, approved_by FROM discrepancy_audit_logs ORDER BY created_at DESC LIMIT 1;'"
```

---

## How it works (data flow)

```
seed_demo.py
  → POST /api/discrepancies/detect
  → graph runs: detect → investigate → propose
  → interrupt before apply_mutation
  → notify node: SlackClient.post_interactive_approval()
      → Slack channel receives Block Kit message with Approve/Reject buttons

User clicks Approve in Slack
  → Slack POSTs to $SERVICE_URL/api/slack/actions (signed with SLACK_SIGNING_SECRET)
  → slack_actions.py verifies signature, extracts run_id
  → resume_workflow(approved=True, reviewer_id=slack_username)
  → graph resumes: apply_mutation → verify → notify → audit
  → Slack resolution message fired
  → audit record written to DB
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "App is not configured for interactivity" | Request URL not set in Slack app | Complete Step 3 |
| HTTP 500 on button click | `Form(...)` consuming stream before body read | See LEARNINGS L005 |
| HTTP 403 on button click | Wrong or missing signing secret | Check `SLACK_SIGNING_SECRET` env var |
| No Slack message fires | Webhook URL not set | Check `SLACK_WEBHOOK_URL` env var |
| Message fires to wrong channel | Webhook scoped to wrong channel | Create new webhook for correct channel (Step 2) |
