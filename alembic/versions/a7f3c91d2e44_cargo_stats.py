"""
create cargo stats table

Revision ID: a7f3c91d2e44
Revises: b0cc0be78aba
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7f3c91d2e44'
down_revision: Union[str, None] = 'b0cc0be78aba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'stats_cargo',

        sa.Column(
            'id',
            sa.Integer(),
            autoincrement=True,
            nullable=False
        ),

        sa.Column(
            'entry_date',
            sa.Date(),
            nullable=False
        ),

        sa.Column(
            'section',
            sa.Text(),
            nullable=False
        ),

        sa.Column(
            'data',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb")
        ),

        sa.Column(
            'updated_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()")
        ),

        sa.PrimaryKeyConstraint('id'),

        sa.UniqueConstraint(
            'entry_date',
            'section',
            name='uq_stats_cargo_entry_date_section'
        )
    )


def downgrade() -> None:
    op.drop_table('stats_cargo')