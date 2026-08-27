"""RP01 Port Overview: rp01_daily_throughput table for the forecast model

Revision ID: rp01dailythr01
Revises: rp02revbd003
Create Date: 2026-08-27
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'rp01dailythr01'
down_revision: Union[str, None] = 'rp02revbd003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Daily equipment-wise throughput, seeded from the 'Eq Wise' sheet by
    # seed_eq_wise.py and read by port_overview/forecast.py. It holds the ~8
    # years of daily history the seasonal model needs; lueu_lines only reaches
    # back about a year, which is far too short to measure a monsoon.
    #
    # equipment is JSONB rather than a fixed column per machine: the fleet
    # changed over the period (BU L-4, BU L-5, Tele Stacker and Excavator only
    # appear in later years), so fixed columns would need a migration every
    # time a machine is added or retired. Nothing queries inside the JSON —
    # the model reads `total` — so there is no index to lose.
    op.execute("""
        CREATE TABLE IF NOT EXISTS rp01_daily_throughput (
            entry_date DATE PRIMARY KEY,
            equipment  JSONB,
            total      NUMERIC(14,3) NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS rp01_daily_throughput')
