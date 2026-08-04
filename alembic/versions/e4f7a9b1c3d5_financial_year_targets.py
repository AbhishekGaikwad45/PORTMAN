"""Create financial year targets table

Revision ID: e4f7a9b1c3d5
Revises: a3f5c81b2d47
Create Date: 2026-08-04
"""

from typing import Sequence, Union
from alembic import op

revision: str = "e4f7a9b1c3d5"
down_revision: Union[str, None] = "a3f5c81b2d47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS financial_year_targets (
            id SERIAL PRIMARY KEY,
            financial_year TEXT NOT NULL UNIQUE,
            targets JSONB NOT NULL DEFAULT '[]'::jsonb,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS financial_year_targets;
    """)