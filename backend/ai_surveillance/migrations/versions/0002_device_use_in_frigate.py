"""Devices: the operator's "Use in Frigate" approval (ADR-0003). Existing devices start switched off.

Revision ID: 0002_device_use_in_frigate
Revises: 0001_baseline
Create Date: 2026-10-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_device_use_in_frigate"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("use_in_frigate", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("use_in_frigate")
