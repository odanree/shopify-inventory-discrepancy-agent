"""Add LLM token usage columns to discrepancy_audit_logs.

Revision ID: 002
Revises: 001
Create Date: 2026-04-08

ADD COLUMN IF NOT EXISTS is idempotent — safe on databases that had these
columns added via inline ALTER TABLE before this migration was introduced.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE discrepancy_audit_logs ADD COLUMN IF NOT EXISTS input_tokens INTEGER")
    op.execute("ALTER TABLE discrepancy_audit_logs ADD COLUMN IF NOT EXISTS output_tokens INTEGER")
    op.execute("ALTER TABLE discrepancy_audit_logs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION")


def downgrade() -> None:
    op.execute("ALTER TABLE discrepancy_audit_logs DROP COLUMN IF EXISTS cost_usd")
    op.execute("ALTER TABLE discrepancy_audit_logs DROP COLUMN IF EXISTS output_tokens")
    op.execute("ALTER TABLE discrepancy_audit_logs DROP COLUMN IF EXISTS input_tokens")
