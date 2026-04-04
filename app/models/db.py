import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow():
    return datetime.now(timezone.utc)


class DiscrepancyAuditLog(Base):
    __tablename__ = "discrepancy_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    sku: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    inventory_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    location_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    discrepancy_pct: Mapped[float] = mapped_column(Float, nullable=False)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution_applied: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_sheets_row_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
