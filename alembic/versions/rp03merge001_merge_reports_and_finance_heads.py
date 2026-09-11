"""Merge the reports and finance branches back into a single head

The graph had forked into two long-lived heads — the reports line
(RP02 revenue backdated → RP01 daily throughput → RP03 permissions) and the
finance line (CNDS01 doc series → HSN input → service_records.billed_quantity →
bill_header TDS/TCS). Both were already applied on the server, so `alembic
current` reported two revisions and plain `alembic upgrade head` refused to
pick one.

This revision has no schema of its own; it exists only to join the two lines so
`head` is unambiguous again.

Revision ID: rp03merge001
Revises: rp03perm001, k9l0m1n2o3p4
Create Date: 2026-09-11
"""
from typing import Sequence, Union

revision: str = 'rp03merge001'
down_revision: Union[str, Sequence[str], None] = ('rp03perm001', 'k9l0m1n2o3p4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
