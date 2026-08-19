"""Allow a 'fdcn' cutover seed (DN/CN start number)

The seed_type CHECK was written when only invoices and bills had a cutover
start number; FDCN01 credit/debit notes now seed the same way, keyed by
(doc_series prefix, financial_year) exactly like invoices.

Revision ID: cut0seedfdcn1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-19
"""
from alembic import op

revision = 'cut0seedfdcn1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE cutover_seed DROP CONSTRAINT IF EXISTS cutover_seed_seed_type_check")
    op.execute("ALTER TABLE cutover_seed ADD CONSTRAINT cutover_seed_seed_type_check "
               "CHECK (seed_type IN ('invoice','bill','fdcn'))")


def downgrade():
    op.execute("DELETE FROM cutover_seed WHERE seed_type = 'fdcn'")
    op.execute("ALTER TABLE cutover_seed DROP CONSTRAINT IF EXISTS cutover_seed_seed_type_check")
    op.execute("ALTER TABLE cutover_seed ADD CONSTRAINT cutover_seed_seed_type_check "
               "CHECK (seed_type IN ('invoice','bill'))")
