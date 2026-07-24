"""pbm01_image_position

Adds an image_position JSONB column to port_berth_master (cx, cy, w, h, angle
in static/img/Clean_berths.png pixel space), and backfills the berths already
positioned via the standalone berth_layout_editor.py tool /
static/data/berth_layout.json that have a matching port_berth_master row
today (BERTH 1-10). BERTH 11/12 were drawn but don't exist as master rows
yet; their positions remain in static/data/berth_layout.json for whoever
adds those rows and re-positions them via PBM01's image picker.

Revision ID: d2e4f6a8b0c2
Revises: b3d5f7a9c1e2
Create Date: 2026-07-23
"""
from typing import Sequence, Union
from alembic import op
from sqlalchemy import text
import json

revision: str = 'd2e4f6a8b0c2'
down_revision: Union[str, None] = 'b3d5f7a9c1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BACKFILL = [
    # label,       cx,     cy,      w,     h,   angle
    ('BERTH 1',  570.2, 1247.9, 101.4,  89.2,  -8.0),
    ('BERTH 2',  558.8, 1152.6,  99.7,  70.0,  -7.0),
    ('BERTH 3',  546.6, 1058.1,  96.2,  80.5,  -8.0),
    ('BERTH 4',  533.4,  953.2, 104.9,  77.0,  -7.0),
    ('BERTH 5',  518.6,  866.6,  96.2,  71.7,  -7.0),
    ('BERTH 6',  512.5,  773.1,  87.5,  77.0,  -9.0),
    ('BERTH 7',  503.7,  695.2,  80.5,  57.7, -15.0),
    ('BERTH 8',  481.0,  622.6,  73.5,  59.5, -16.0),
    ('BERTH 9',  464.4,  553.6,  71.7,  57.7, -14.0),
    ('BERTH 10', 435.5,  467.9,  56.0,  68.2, -17.0),
    # BERTH 11 / BERTH 12 skipped: no matching port_berth_master row yet.
]


def upgrade() -> None:
    op.execute('ALTER TABLE port_berth_master ADD COLUMN IF NOT EXISTS image_position JSONB')

    conn = op.get_bind()
    for label, cx, cy, w, h, angle in _BACKFILL:
        pos = json.dumps({'cx': cx, 'cy': cy, 'w': w, 'h': h, 'angle': angle})
        conn.execute(text(
            f"UPDATE port_berth_master SET image_position = '{pos}'::jsonb "
            f"WHERE UPPER(TRIM(berth_name)) = '{label}'"
        ))


def downgrade() -> None:
    op.execute('ALTER TABLE port_berth_master DROP COLUMN IF EXISTS image_position')
