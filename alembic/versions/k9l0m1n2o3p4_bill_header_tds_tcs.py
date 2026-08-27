"""Roll TDS/TCS up onto bill_header

bill_lines have carried tds_amount / tcs_amount since the FSTM01 flags went in,
but the header had no column for them, so a bill whose service is TCS-liable
showed no TCS anywhere (invoice_header already has both columns).

Revision ID: k9l0m1n2o3p4
Revises: j8k9l0m1n2o3
Create Date: 2026-08-27
"""
from alembic import op

revision = 'k9l0m1n2o3p4'
down_revision = 'j8k9l0m1n2o3'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE bill_header
            ADD COLUMN IF NOT EXISTS tds_amount NUMERIC(18,2) DEFAULT 0,
            ADD COLUMN IF NOT EXISTS tcs_amount NUMERIC(18,2) DEFAULT 0
    """)
    # Backfill from the lines, which already hold the computed amounts.
    # total_amount now includes TCS (it is collected from the customer), so
    # existing bills are restated to match — bills are internal documents,
    # unlike issued invoices, which are left exactly as posted.
    op.execute("""
        UPDATE bill_header bh
        SET tds_amount = live.tds,
            tcs_amount = live.tcs,
            total_amount = ROUND(COALESCE(bh.subtotal, 0)
                                 + COALESCE(bh.cgst_amount, 0)
                                 + COALESCE(bh.sgst_amount, 0)
                                 + COALESCE(bh.igst_amount, 0)
                                 + live.tcs, 2)
        FROM (SELECT bill_id,
                     COALESCE(SUM(tds_amount), 0) AS tds,
                     COALESCE(SUM(tcs_amount), 0) AS tcs
              FROM bill_lines GROUP BY bill_id) live
        WHERE bh.id = live.bill_id
          AND bh.bill_status <> 'Invoiced'
    """)


def downgrade():
    op.execute("ALTER TABLE bill_header DROP COLUMN IF EXISTS tcs_amount")
    op.execute("ALTER TABLE bill_header DROP COLUMN IF EXISTS tds_amount")
