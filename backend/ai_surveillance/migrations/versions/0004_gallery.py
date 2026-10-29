"""Authorized-person gallery: people, reference faces, audit log (ADR-0004, Phase 5).

Revision ID: 0004_gallery
Revises: 0003_face_events
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_gallery"
down_revision = "0003_face_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("notes", sa.String(500), nullable=True),
        sa.Column("consent_date", sa.Date, nullable=False),
        sa.Column("consent_note", sa.String(500), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "reference_faces",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("person_id", sa.String(32), sa.ForeignKey("people.id", ondelete="CASCADE"), nullable=False),
        sa.Column("image_file", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(16), nullable=False),
        sa.Column("image_width", sa.Integer, nullable=False),
        sa.Column("image_height", sa.Integer, nullable=False),
        sa.Column("box_x", sa.Integer, nullable=False),
        sa.Column("box_y", sa.Integer, nullable=False),
        sa.Column("box_w", sa.Integer, nullable=False),
        sa.Column("box_h", sa.Integer, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("keypoints", sa.Text, nullable=False),
        sa.Column("quality", sa.Text, nullable=False),
        sa.Column("detector", sa.String(64), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reference_faces_person_id", "reference_faces", ["person_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(32), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])


def downgrade() -> None:
    # Rows go with the tables; encrypted image files under media/gallery stay on disk (see DATABASE.md).
    op.drop_index("ix_audit_log_at", "audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_reference_faces_person_id", "reference_faces")
    op.drop_table("reference_faces")
    op.drop_table("people")
