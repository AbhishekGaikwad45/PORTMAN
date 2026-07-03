"""RP01: add cargo_type_a / cargo_type_b / mv_mbc to historical LUEU, and
create barge_position_report table.

Revision ID: a1c3e5f7b9d0
Revises: e6b7c8d9e0f1
Create Date: 2026-06-29
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'a1c3e5f7b9d0'
down_revision: Union[str, None] = 'e6b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive, nullable → existing rows untouched.
    op.execute("ALTER TABLE rp01_historical_lueu ADD COLUMN IF NOT EXISTS cargo_type_a VARCHAR(200)")
    op.execute("ALTER TABLE rp01_historical_lueu ADD COLUMN IF NOT EXISTS cargo_type_b VARCHAR(200)")
    op.execute("ALTER TABLE rp01_historical_lueu ADD COLUMN IF NOT EXISTS mv_mbc       VARCHAR(300)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS barge_position_report (
            id                    SERIAL PRIMARY KEY,
            report_date           DATE NOT NULL,
            shift                 VARCHAR(5) NOT NULL,
            shift_incharge        TEXT DEFAULT '',
            bpo                   TEXT DEFAULT '',
            crane_operator        TEXT DEFAULT '',
            berth_layout          JSONB DEFAULT '[]'::jsonb,
            waiting_area          JSONB DEFAULT '[]'::jsonb,
            wt_r19                JSONB DEFAULT '{}'::jsonb,
            mbc_eta               JSONB DEFAULT '{}'::jsonb,
            eta_to_dharamtar      JSONB DEFAULT '{}'::jsonb,
            on_the_way_gull       JSONB DEFAULT '{}'::jsonb,
            shift_plan            JSONB DEFAULT '{}'::jsonb,
            notes                 JSONB DEFAULT '[]'::jsonb,
            movement_logs         JSONB DEFAULT '[]'::jsonb,
            created_at            TIMESTAMPTZ DEFAULT NOW(),
            updated_at            TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT uq_barge_position_report
                UNIQUE (report_date, shift)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS barge_position_report")
    op.execute("ALTER TABLE rp01_historical_lueu DROP COLUMN IF EXISTS mv_mbc")
    op.execute("ALTER TABLE rp01_historical_lueu DROP COLUMN IF EXISTS cargo_type_b")
    op.execute("ALTER TABLE rp01_historical_lueu DROP COLUMN IF EXISTS cargo_type_a")
