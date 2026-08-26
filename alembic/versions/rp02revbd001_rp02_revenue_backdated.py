"""RP02 Revenue Register: rp02_revenue_backdated table

Revision ID: rp02revbd001
Revises: a7f3c91d2e44
Create Date: 2026-08-26
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'rp02revbd001'
down_revision: Union[str, None] = 'a7f3c91d2e44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same shape as the Revenue Register grid, minus Days/Bucket — those are
    # derived from invoice_date at read time and would go stale if stored.
    # ack_date stays VARCHAR: legacy registers mix real dates with free text.
    op.execute("""
        CREATE TABLE IF NOT EXISTS rp02_revenue_backdated (
            id               SERIAL PRIMARY KEY,
            invoice_no       VARCHAR(100) NOT NULL,
            group_type       VARCHAR(50),
            revenue_type_1   VARCHAR(100),
            revenue_type_2   VARCHAR(100),
            cargo_volume     NUMERIC,
            invoice_date     DATE NOT NULL,
            cust_code        VARCHAR(50),
            customer_name    VARCHAR(300) NOT NULL,
            gl_code          VARCHAR(50),
            grouping_label   VARCHAR(300),
            qty              NUMERIC,
            rate             NUMERIC,
            tax_category     VARCHAR(30),
            tax_rate         NUMERIC,
            basic_value      NUMERIC,
            sgst             NUMERIC,
            cgst             NUMERIC,
            igst             NUMERIC,
            invoice_value    NUMERIC,
            gstin            VARCHAR(30),
            sap_doc_no       VARCHAR(50),
            sac_code         VARCHAR(30),
            hsn_code         VARCHAR(30),
            irn              VARCHAR(120),
            ack_date         VARCHAR(30),
            ack_no           VARCHAR(50),
            barcode          VARCHAR(100),
            booking_status   VARCHAR(100),
            tds_tcs          NUMERIC,
            net_receivable   NUMERIC,
            uploaded_by      INTEGER,
            uploaded_at      TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_rp02_rev_bd_date ON rp02_revenue_backdated (invoice_date)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rp02_rev_bd_invoice ON rp02_revenue_backdated (invoice_no)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rp02_revenue_backdated")
