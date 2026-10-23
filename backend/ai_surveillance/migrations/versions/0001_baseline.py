"""Baseline: the schema as created before migrations existed.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "login_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "captures",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("camera", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("file_name", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.Integer, nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("frigate_export_id", sa.String(128), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("rtsp_port", sa.Integer, nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("password_enc", sa.String(512), nullable=True),
        sa.Column("main_path", sa.String(512), nullable=False),
        sa.Column("sub_path", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_detail", sa.String(500), nullable=True),
        sa.Column("test_result", sa.Text, nullable=True),
        sa.Column("tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("devices")
    op.drop_table("captures")
    op.drop_table("login_sessions")
    op.drop_table("admin_users")
