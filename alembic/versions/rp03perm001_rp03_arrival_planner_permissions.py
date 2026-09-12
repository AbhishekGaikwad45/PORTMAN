"""RP03 Arrival Planner: module permissions

RP03 reads existing LDUD01 / MBC01 tables and creates none of its own, so this
migration only grants access — read for everyone, full for admins, the same
pattern RP02 used.

Revision ID: rp03perm001
Revises: rp01dailythr01
Create Date: 2026-09-11
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'rp03perm001'
down_revision: Union[str, None] = 'rp01dailythr01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('''
        INSERT INTO module_permissions (user_id, module_code, can_read, can_add, can_edit, can_delete)
        SELECT DISTINCT u.id, 'RP03',
            1,
            CASE WHEN u.is_admin = 1 THEN 1 ELSE 0 END,
            CASE WHEN u.is_admin = 1 THEN 1 ELSE 0 END,
            CASE WHEN u.is_admin = 1 THEN 1 ELSE 0 END
        FROM users u
        ON CONFLICT (user_id, module_code) DO NOTHING
    ''')


def downgrade() -> None:
    op.execute("DELETE FROM module_permissions WHERE module_code = 'RP03'")
