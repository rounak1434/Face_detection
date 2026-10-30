"""Face events and face detections from Frigate person events (ADR-0004, Phase 3).

Revision ID: 0003_face_events
Revises: 0002_device_use_in_frigate
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_face_events"
down_revision = "0002_device_use_in_frigate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "face_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("frigate_event_id", sa.String(30), nullable=False, unique=True),
        sa.Column("camera", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("face_count", sa.Integer, nullable=False),
        sa.Column("frame_source", sa.String(16), nullable=True),
        sa.Column("frame_time", sa.Float, nullable=True),
        sa.Column("frame_file", sa.String(64), nullable=True),
        sa.Column("frame_width", sa.Integer, nullable=True),
        sa.Column("frame_height", sa.Integer, nullable=True),
        sa.Column("detector", sa.String(64), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_face_events_camera", "face_events", ["camera"])
    op.create_index("ix_face_events_started_at", "face_events", ["started_at"])
    op.create_index("ix_face_events_status", "face_events", ["status"])
    op.create_table(
        "face_detections",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("event_id", sa.String(32), sa.ForeignKey("face_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("box_x", sa.Integer, nullable=False),
        sa.Column("box_y", sa.Integer, nullable=False),
        sa.Column("box_w", sa.Integer, nullable=False),
        sa.Column("box_h", sa.Integer, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("keypoints", sa.Text, nullable=False),
        sa.Column("quality", sa.Text, nullable=False),
        sa.Column("crop_file", sa.String(64), nullable=True),
    )
    op.create_index("ix_face_detections_event_id", "face_detections", ["event_id"])


def downgrade() -> None:
    # Rows go with the tables; image files under media/faces stay on disk (see DATABASE.md).
    op.drop_index("ix_face_detections_event_id", "face_detections")
    op.drop_table("face_detections")
    op.drop_index("ix_face_events_status", "face_events")
    op.drop_index("ix_face_events_started_at", "face_events")
    op.drop_index("ix_face_events_camera", "face_events")
    op.drop_table("face_events")
