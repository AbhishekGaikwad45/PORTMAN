"""RP03 Arrival Planner: berth / route / priority plan

One table, owned entirely by RP03. Nothing outside the module reads or writes
it: the operational records (mbc_discharge_port_lines.vessel_unloading_berth,
lueu_lines.route_name, the milestone columns) still capture what actually
happened, independently. This table only records what planners intend, so
dropping it returns RP03 to the read-only board it was.

trip_id has no foreign key on purpose — it points at ldud_barge_lines.id or
mbc_header.id depending on trip_kind, and a plan row orphaned by a deleted trip
simply never renders.

Revision ID: rp03plan001
Revises: rp03merge001
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'rp03plan001'
down_revision: Union[str, None] = 'rp03merge001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('''
        CREATE TABLE IF NOT EXISTS rp03_arrival_plan (
            id         SERIAL PRIMARY KEY,
            trip_kind  TEXT NOT NULL CHECK (trip_kind IN ('BARGE', 'MBC')),
            trip_id    INTEGER NOT NULL,
            berth_id   INTEGER REFERENCES port_berth_master(id) ON DELETE SET NULL,
            route_id   INTEGER REFERENCES conveyor_routes(id) ON DELETE SET NULL,
            priority   INTEGER,
            planned_at TIMESTAMP,
            remarks    TEXT,
            updated_by TEXT,
            updated_at TIMESTAMP,
            UNIQUE (trip_kind, trip_id)
        )
    ''')


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS rp03_arrival_plan')
