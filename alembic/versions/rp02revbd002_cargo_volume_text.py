"""RP02 backdated revenue: Cargo Volume is a YES/NO flag, not a quantity

Revision ID: rp02revbd002
Revises: rp02revbd001
Create Date: 2026-08-26
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'rp02revbd002'
down_revision: Union[str, None] = 'rp02revbd001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The register sheet carries YES/NO in Cargo Volume, so NUMERIC rejected
    # every real file. Any rows already loaded hold numbers, which cast cleanly.
    op.execute("""
        ALTER TABLE rp02_revenue_backdated
        ALTER COLUMN cargo_volume TYPE VARCHAR(20)
        USING cargo_volume::VARCHAR(20)
    """)


def downgrade() -> None:
    # Only rows that still look numeric survive the way back.
    op.execute(r"""
        ALTER TABLE rp02_revenue_backdated
        ALTER COLUMN cargo_volume TYPE NUMERIC
        USING NULLIF(regexp_replace(cargo_volume, '[^0-9.\-]', '', 'g'), '')::NUMERIC
    """)
