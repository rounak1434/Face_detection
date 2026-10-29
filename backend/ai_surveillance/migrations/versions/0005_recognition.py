"""Recognition: switch setting, reference embeddings, match outcomes (ADR-0004, Phase 6).

Revision ID: 0005_recognition
Revises: 0004_gallery
Create Date: 2026-10-30

All new columns are nullable: events and reference faces stored before this revision keep
null outcomes and no embedding (embeddings are computed from the stored reference images).
SQLite cannot add a foreign key in place, so face_detections (a child table: nothing refers
to it) is rebuilt in batch mode and its rows are copied. The other tables only gain columns,
which SQLite adds in place; they are never rebuilt (see downgrade).
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_recognition"
down_revision = "0004_gallery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
    )
    with op.batch_alter_table("reference_faces") as batch:
        batch.add_column(sa.Column("embedding_enc", sa.LargeBinary, nullable=True))
        batch.add_column(sa.Column("embedder", sa.String(64), nullable=True))
    with op.batch_alter_table("face_events") as batch:
        batch.add_column(sa.Column("recognition_state", sa.String(16), nullable=True))
        batch.add_column(sa.Column("recognizer", sa.String(64), nullable=True))
    with op.batch_alter_table("face_detections") as batch:
        batch.add_column(sa.Column("outcome", sa.String(16), nullable=True))
        batch.add_column(sa.Column("person_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("similarity", sa.Float, nullable=True))
        batch.add_column(sa.Column("accept_threshold", sa.Float, nullable=True))
        batch.add_column(sa.Column("reject_threshold", sa.Float, nullable=True))
        batch.create_foreign_key("fk_face_detections_person_id_people", "people", ["person_id"], ["id"],
                                 ondelete="SET NULL")
        batch.create_index("ix_face_detections_outcome", ["outcome"])
        batch.create_index("ix_face_detections_person_id", ["person_id"])


def downgrade() -> None:
    # Match outcomes, the switch setting and the encrypted reference embeddings are dropped;
    # reference images, people and face events are kept.
    with op.batch_alter_table("face_detections") as batch:
        batch.drop_index("ix_face_detections_person_id")
        batch.drop_index("ix_face_detections_outcome")
        batch.drop_constraint("fk_face_detections_person_id_people", type_="foreignkey")
        for column in ("reject_threshold", "accept_threshold", "similarity", "person_id", "outcome"):
            batch.drop_column(column)
    # Native DROP COLUMN (SQLite 3.35+), not a batch rebuild: rebuilding face_events would drop
    # and recreate a parent table while foreign keys are enforced, deleting its detections.
    for table, column in (("face_events", "recognizer"), ("face_events", "recognition_state"),
                          ("reference_faces", "embedder"), ("reference_faces", "embedding_enc")):
        op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    op.drop_table("app_settings")
