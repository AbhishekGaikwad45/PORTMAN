"""Add requires_hsn_input flag to finance_service_types

Services whose HSN/SAC is not fixed (varies per bill) get this flag set;
FIN01 then asks for the HSN on each bill line instead of copying the
master sac_code.

Revision ID: i7j8k9l0m1n2
Revises: a3f5c81b2d47
Create Date: 2026-08-07
"""
from alembic import op

revision = 'i7j8k9l0m1n2'
down_revision = 'a3f5c81b2d47'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE finance_service_types
            ADD COLUMN IF NOT EXISTS requires_hsn_input SMALLINT DEFAULT 0
    """)


def downgrade():
    op.execute("ALTER TABLE finance_service_types DROP COLUMN IF EXISTS requires_hsn_input")
