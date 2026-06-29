"""Allow multiple cargo per (barge, trip) on ldud_barge_lines

The previous unique index `uq_ldud_barge_trip` on
(ldud_id, barge_name, trip_number) forbade a barge carrying more than one
cargo on a single trip — but that is a real scenario (one barge trip with
mixed cargo, each cargo a separate discharge line), and the "+C / add
another cargo for this same trip" button depends on it. The strict index
made that button fail with "Trip N already exists for barge ...".

Fix: replace the index with one that also keys on cargo_name, so distinct
cargo on the same trip is allowed while a true duplicate (same barge, same
trip, same cargo) is still blocked. cargo_name NULL placeholder rows remain
allowed (Postgres treats NULLs as distinct in unique indexes).

The old index was stricter, so no (barge, trip, cargo) duplicates can exist
today — no renumbering needed.

Revision ID: c4f5a6b7c8d9
Revises: c3e4f5a6b7c8
Create Date: 2026-06-26
"""
from typing import Sequence, Union
from alembic import op


revision: str = 'c4f5a6b7c8d9'
down_revision: Union[str, None] = 'c3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('DROP INDEX IF EXISTS uq_ldud_barge_trip')
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ldud_barge_trip_cargo
            ON ldud_barge_lines (ldud_id, barge_name, trip_number, cargo_name)
            WHERE barge_name IS NOT NULL
              AND TRIM(barge_name) <> ''
              AND trip_number IS NOT NULL
    """)


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS uq_ldud_barge_trip_cargo')
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ldud_barge_trip
            ON ldud_barge_lines (ldud_id, barge_name, trip_number)
            WHERE barge_name IS NOT NULL
              AND TRIM(barge_name) <> ''
              AND trip_number IS NOT NULL
    """)
