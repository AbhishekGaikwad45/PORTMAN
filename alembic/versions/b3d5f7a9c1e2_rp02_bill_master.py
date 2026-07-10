"""RP02 Finance Reports: rp02_bill_master table + module permissions

Revision ID: b3d5f7a9c1e2
Revises: a1c3e5f7b9d0
Create Date: 2026-07-10
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'b3d5f7a9c1e2'
down_revision: Union[str, None] = 'a1c3e5f7b9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Discharge columns are VARCHAR on purpose: prod data mixes real timestamps
    # ('25-03-2026 06:30', '14.04.2026 19:00') with free text ('Under Discharge').
    op.execute("""
        CREATE TABLE IF NOT EXISTS rp02_bill_master (
            id                   SERIAL PRIMARY KEY,
            financial_year       VARCHAR(20) NOT NULL,
            month_label          VARCHAR(20),
            vessel_name          VARCHAR(300) NOT NULL,
            material_po          VARCHAR(100),
            customer_name        VARCHAR(300) NOT NULL,
            cargo_type           VARCHAR(100),
            cargo_name           VARCHAR(200),
            bl_qty               NUMERIC,
            load_port            VARCHAR(200),
            mv_mbc               VARCHAR(10),
            discharge_commence   VARCHAR(50),
            discharge_completed  VARCHAR(50),
            bill_no              VARCHAR(100),
            credit_note          VARCHAR(100),
            old_bill             VARCHAR(100),
            uploaded_by          INTEGER,
            uploaded_at          TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_rp02_bill_master_fy ON rp02_bill_master (financial_year)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rp02_bill_master_customer ON rp02_bill_master (customer_name)")

    # Seed RP02 permissions: read for everyone, full access for admins
    # (same pattern as f6g7h8i9j0k1_add_new_module_permissions).
    op.execute('''
        INSERT INTO module_permissions (user_id, module_code, can_read, can_add, can_edit, can_delete)
        SELECT DISTINCT u.id, 'RP02',
            1,
            CASE WHEN u.is_admin = 1 THEN 1 ELSE 0 END,
            CASE WHEN u.is_admin = 1 THEN 1 ELSE 0 END,
            CASE WHEN u.is_admin = 1 THEN 1 ELSE 0 END
        FROM users u
        ON CONFLICT (user_id, module_code) DO NOTHING
    ''')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rp02_bill_master")
    op.execute("DELETE FROM module_permissions WHERE module_code = 'RP02'")
