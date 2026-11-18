"""create DMIS01 diesel mis tables and heme master

Revision ID: dmis01merge001
Revises: tgu01merge001
Create Date: 2026-11-17

Creates:
- dmis01_diesel_consumption (Date, Equipment, Start Reading, End Reading, Difference, Diesel Filled, Rate, Remarks, Created By)
- dmis01_diesel_received (Date, Diesel Received, Created By)
- dmis01_tank_stock (Date, Equipment, Month-start Tank Stock, Created By)
- heme_master (type, model, number, created_by, created_at, is_deleted)
"""

from typing import Sequence, Union
from alembic import op

revision: str = "dmis01merge001"
down_revision: Union[str, None] = "tgu01merge001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Diesel Consumption Table
    op.execute("""
        CREATE TABLE IF NOT EXISTS dmis01_diesel_consumption (
            id SERIAL PRIMARY KEY,
            entry_date TEXT NOT NULL,
            equipment TEXT NOT NULL,
            start_reading TEXT,
            end_reading TEXT,
            difference TEXT,
            diesel_filled TEXT,
            rate TEXT,
            remarks TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_deleted BOOLEAN DEFAULT FALSE
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_dmis01_cons_equip 
        ON dmis01_diesel_consumption(equipment, entry_date DESC) 
        WHERE is_deleted = FALSE
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_dmis01_cons_date 
        ON dmis01_diesel_consumption(entry_date DESC, equipment) 
        WHERE is_deleted = FALSE
    """)

    # 2. Diesel Received Table
    op.execute("""
        CREATE TABLE IF NOT EXISTS dmis01_diesel_received (
            id SERIAL PRIMARY KEY,
            received_date TEXT NOT NULL,
            diesel_received TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_deleted BOOLEAN DEFAULT FALSE
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_dmis01_recd_date 
        ON dmis01_diesel_received(received_date DESC) 
        WHERE is_deleted = FALSE
    """)

    # 3. Tank Stock Table
    op.execute("""
        CREATE TABLE IF NOT EXISTS dmis01_tank_stock (
            id SERIAL PRIMARY KEY,
            stock_date TEXT NOT NULL,
            equipment TEXT NOT NULL,
            month_start_tank_stock TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_deleted BOOLEAN DEFAULT FALSE,
            UNIQUE (stock_date, equipment)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_dmis01_stock_lookup 
        ON dmis01_tank_stock(stock_date, equipment) 
        WHERE is_deleted = FALSE
    """)

    # 4. HEME Master Table (Heavy Earth Moving Equipment)
    op.execute("""
        CREATE TABLE IF NOT EXISTS heme_master (
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL,
            model TEXT,
            number TEXT NOT NULL,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_deleted BOOLEAN DEFAULT FALSE
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS heme_master")

    op.execute("DROP INDEX IF EXISTS idx_dmis01_stock_lookup")
    op.execute("DROP TABLE IF EXISTS dmis01_tank_stock")

    op.execute("DROP INDEX IF EXISTS idx_dmis01_recd_date")
    op.execute("DROP TABLE IF EXISTS dmis01_diesel_received")

    op.execute("DROP INDEX IF EXISTS idx_dmis01_cons_date")
    op.execute("DROP INDEX IF EXISTS idx_dmis01_cons_equip")
    op.execute("DROP TABLE IF EXISTS dmis01_diesel_consumption")
