"""RP02 backdated revenue: store every column as text

Revision ID: rp02revbd003
Revises: rp02revbd002
Create Date: 2026-08-26
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'rp02revbd003'
down_revision: Union[str, None] = 'rp02revbd002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of the schema at this revision — deliberately not imported from
# the module, so a later column change can't rewrite history.
_COLUMNS = [
    'invoice_no', 'group_type', 'revenue_type_1', 'revenue_type_2',
    'cargo_volume', 'invoice_date', 'cust_code', 'customer_name', 'gl_code',
    'grouping_label', 'qty', 'rate', 'tax_category', 'tax_rate', 'basic_value',
    'sgst', 'cgst', 'igst', 'invoice_value', 'gstin', 'sap_doc_no', 'sac_code',
    'hsn_code', 'irn', 'ack_date', 'ack_no', 'barcode', 'booking_status',
    'tds_tcs', 'net_receivable',
]


def upgrade() -> None:
    # A backdated register is a spreadsheet, not a ledger: it carries YES/NO in
    # numeric-looking columns, blanks, notes and mixed date formats. Storing it
    # verbatim as text means no file is ever rejected over a cell's shape.
    # Dates are normalised to YYYY-MM-DD on the way in where they parse, which
    # is what the month key and the ageing columns read.
    op.execute('ALTER TABLE rp02_revenue_backdated ' + ', '.join(
        'ALTER COLUMN {0} TYPE TEXT USING {0}::TEXT'.format(c) for c in _COLUMNS))
    op.execute('ALTER TABLE rp02_revenue_backdated '
               'ALTER COLUMN invoice_date DROP NOT NULL, '
               'ALTER COLUMN customer_name DROP NOT NULL')


def downgrade() -> None:
    op.execute("""
        ALTER TABLE rp02_revenue_backdated
        ALTER COLUMN invoice_date TYPE DATE USING NULLIF(invoice_date, '')::DATE,
        ALTER COLUMN customer_name TYPE VARCHAR(300)
    """)
