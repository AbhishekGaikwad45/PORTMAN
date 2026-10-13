"""create all MBCT01 tables

Revision ID: mbct01merge001
Revises: rp01poviews001
Create Date: 2026-09-28

Creates:
- mbct01_header
- mbct01_anchorage
- mbct01_port_activity
- mbct01_tug_activity
"""

from typing import Sequence, Union


from alembic import op


# revision identifiers, used by Alembic.
revision: str = "mbct01merge001"
down_revision: Union[str, None] = "rp01poviews001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    
    # 1. MBCT01 HEADER
  
    op.execute("""
        CREATE TABLE IF NOT EXISTS mbct01_header (
            id SERIAL PRIMARY KEY,
            mbc_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY (mbc_id) REFERENCES mbc_header(id) ON DELETE CASCADE
        )
    """)

    
    # 2. MBCT01 ANCHORAGE
  
    op.execute("""
        CREATE TABLE IF NOT EXISTS mbct01_anchorage (
            id SERIAL PRIMARY KEY,
            mbc_id INTEGER NOT NULL,
            eta_gull TEXT,
            nor_tendered_green_buoy TEXT,
            anchor_dropped_gull TEXT,
            anchor_away_gull TEXT,
            passing_gull_island TEXT,
            passing_high_tension TEXT,
            passing_yellow_crane TEXT,
            remarks TEXT,
            FOREIGN KEY (mbc_id) REFERENCES mbc_header(id) ON DELETE CASCADE
        )
    """)

   
    # 3. MBCT01 PORT ACTIVITY
   
    op.execute("""
        CREATE TABLE IF NOT EXISTS mbct01_port_activity (
            id SERIAL PRIMARY KEY,
            mbc_id INTEGER NOT NULL,

            activity_id TEXT,
            tugs TEXT,
            first_line_ashore_cast_off TEXT,
            all_made_fast_cast_off TEXT,
            shore_supply_starts TEXT,
            shore_supply_ends TEXT,
            from_berth_anchorage_id TEXT,
            to_berth_anchorage_id TEXT,
            loaded_condition TEXT,
            mbc_position TEXT,
            reasons TEXT,
            FOREIGN KEY (mbc_id) REFERENCES mbc_header(id) ON DELETE CASCADE
        )
    """)

   
    # 4. MBCT01 TUG ACTIVITY
   
    op.execute("""
        CREATE TABLE IF NOT EXISTS mbct01_tug_activity (
            id SERIAL PRIMARY KEY,
            port_activity_id INTEGER NOT NULL,
            tug_id TEXT NOT NULL,
            tug_made_fast TEXT,
            tug_cast_off TEXT,
            engine_start TEXT,
            engine_stop TEXT,
            t_running_hours VARCHAR(50),
            fuel_used VARCHAR(50),
            remark TEXT,
            FOREIGN KEY (port_activity_id) REFERENCES mbct01_port_activity(id) ON DELETE CASCADE,
            UNIQUE (port_activity_id, tug_id)
        )
    """)


def downgrade() -> None:
    # 1. DROP TUG ACTIVITY
    op.execute("""DROP TABLE IF EXISTS mbct01_tug_activity""")
    # 2. DROP PORT ACTIVITY
    op.execute("""DROP TABLE IF EXISTS mbct01_port_activity""")
    # 3. DROP ANCHORAGE
    op.execute("""DROP TABLE IF EXISTS mbct01_anchorage""")
    # 4. DROP HEADER
    op.execute("""DROP TABLE IF EXISTS mbct01_header""")
