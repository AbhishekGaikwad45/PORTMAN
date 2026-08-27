"""Add billed_quantity to service_records (partial billing of services)

Mirrors the cargo declaration counters: FIN01 section B can now bill part of
a service record's quantity, the rest stays as balance for the next bill.

Revision ID: j8k9l0m1n2o3
Revises: i7j8k9l0m1n2
Create Date: 2026-08-27
"""
from alembic import op

revision = 'j8k9l0m1n2o3'
down_revision = 'i7j8k9l0m1n2'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE service_records
            ADD COLUMN IF NOT EXISTS billed_quantity NUMERIC(18,3) DEFAULT 0
    """)
    # Existing all-or-nothing billed records: the whole quantity was billed.
    op.execute("""
        UPDATE service_records
        SET billed_quantity = COALESCE(billable_quantity, 0)
        WHERE COALESCE(is_billed, 0) = 1 AND COALESCE(billed_quantity, 0) = 0
    """)


def downgrade():
    op.execute("ALTER TABLE service_records DROP COLUMN IF EXISTS billed_quantity")
