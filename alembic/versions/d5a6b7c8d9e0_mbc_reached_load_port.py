"""Add reached_load_port to mbc_discharge_port_lines

New "Reached Load Port" datetime field at the end of the MBC01 discharge
port details sub-table. Stored as TEXT to match the other datetime columns
on this table (YYYY-MM-DDTHH:MM strings).

Revision ID: d5a6b7c8d9e0
Revises: c4f5a6b7c8d9
Create Date: 2026-06-26
"""
from typing import Sequence, Union
from alembic import op


revision: str = 'd5a6b7c8d9e0'
down_revision: Union[str, None] = 'c4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('ALTER TABLE mbc_discharge_port_lines ADD COLUMN IF NOT EXISTS reached_load_port TEXT')


def downgrade() -> None:
    op.execute('ALTER TABLE mbc_discharge_port_lines DROP COLUMN IF EXISTS reached_load_port')
