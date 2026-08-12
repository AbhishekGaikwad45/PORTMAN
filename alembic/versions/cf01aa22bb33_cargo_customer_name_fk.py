"""Enforce the cargo -> customer link with a real FK.

customer_name on the three declaration tables was loose text, matched against
vessel_customers.name only at billing time (FIN01 views.py:776/792/808). Rename
a customer in VCUM01 and every cargo row kept the old string, then silently
vanished from Generate Bill — no error, no warning, just missing invoices.

ON UPDATE CASCADE makes a rename carry its cargo along. ON DELETE RESTRICT
closes the other orphan path. Unknown names now fail at write time in
MBC01/VCN01 instead of silently at billing time.

Also merges the two open heads (d0e1f2a3b4c5, d2e4f6a8b0c2) so `alembic
upgrade head` resolves to a single head again.

This migration ABORTS, without changing anything, if any cargo row points at a
customer that is not in vessel_customers — the error lists those rows. That is
deliberate: they are the rows currently missing from billing, and they need a
human decision (correct the name, or add the master row) before the constraint
can be trusted.

Revision ID: cf01aa22bb33
Revises: d0e1f2a3b4c5, d2e4f6a8b0c2
Create Date: 2026-08-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'cf01aa22bb33'
down_revision: Union[str, Sequence[str], None] = ('d0e1f2a3b4c5', 'd2e4f6a8b0c2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# declaration table -> a column printed alongside an orphan so ops can find the row
TABLES = {
    'mbc_customer_details':         'material_po',
    'vcn_cargo_declaration':        'bl_no',
    'vcn_export_cargo_declaration': 'bl_no',
}


def upgrade() -> None:
    conn = op.get_bind()

    # --- preflight: duplicate names would block the UNIQUE constraint ---
    dupes = conn.exec_driver_sql(
        'SELECT name FROM vessel_customers GROUP BY name HAVING COUNT(*) > 1'
    ).fetchall()
    if dupes:
        raise RuntimeError(
            'vessel_customers has duplicate names, merge them first: '
            + ', '.join(repr(d[0]) for d in dupes)
        )

    # --- normalise blanks: a FK ignores NULL but rejects '' ---
    for table in TABLES:
        conn.exec_driver_sql(
            f"UPDATE {table} SET customer_name = NULL "
            f"WHERE TRIM(COALESCE(customer_name, '')) = ''"
        )

    # --- preflight: orphaned cargo. Fail with the list rather than a bare FK
    # violation. Rolls back the blank normalisation above with it. ---
    orphans = []
    for table, ref_col in TABLES.items():
        rows = conn.exec_driver_sql(f"""
            SELECT id, customer_name, {ref_col} AS ref
            FROM {table} cd
            WHERE customer_name IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM vessel_customers vc WHERE vc.name = cd.customer_name)
            ORDER BY id
        """).fetchall()
        orphans += [f'{table} id={r[0]} customer_name={r[1]!r} ref={r[2]!r}' for r in rows]
    if orphans:
        raise RuntimeError(
            'Cargo rows point at a customer that is not in vessel_customers. Fix the '
            'name (or add the missing master row) and re-run — these are exactly the '
            'rows currently missing from FIN01 Generate Bill:\n  '
            + '\n  '.join(orphans)
        )

    # --- the fix. Guarded so a re-run is a no-op; Postgres has no
    # ADD CONSTRAINT IF NOT EXISTS. ---
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'vessel_customers_name_key') THEN
                ALTER TABLE vessel_customers ADD CONSTRAINT vessel_customers_name_key UNIQUE (name);
            END IF;
        END $$;
    """)

    for table in TABLES:
        op.execute(f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{table}_customer_fk') THEN
                    ALTER TABLE {table}
                        ADD CONSTRAINT {table}_customer_fk
                        FOREIGN KEY (customer_name) REFERENCES vessel_customers (name)
                        ON UPDATE CASCADE ON DELETE RESTRICT;
                END IF;
            END $$;
        """)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_customer_fk')
    op.execute('ALTER TABLE vessel_customers DROP CONSTRAINT IF EXISTS vessel_customers_name_key')
