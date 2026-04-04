"""Initial schema — discrepancy_audit_logs

Revision ID: 001
Revises:
Create Date: 2026-04-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discrepancy_audit_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False, index=True),
        sa.Column("sku", sa.String(128), nullable=False, index=True),
        sa.Column("inventory_item_id", sa.String(128), nullable=False),
        sa.Column("location_id", sa.String(128), nullable=False),
        sa.Column("expected_qty", sa.Integer, nullable=False),
        sa.Column("actual_qty", sa.Integer, nullable=False),
        sa.Column("discrepancy_pct", sa.Float, nullable=False),
        sa.Column("root_cause", sa.Text, nullable=True),
        sa.Column("proposed_action", sa.String(64), nullable=True),
        sa.Column("approved", sa.Boolean, nullable=True),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("resolution_applied", sa.String(128), nullable=True),
        sa.Column("resolution_notes", sa.Text, nullable=True),
        sa.Column("google_sheets_row_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("discrepancy_audit_logs")
