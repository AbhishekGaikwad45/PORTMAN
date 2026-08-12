"""collapse heads

Revision ID: 39681ca62c54
Revises: e4f7a9b1c3d5, i7j8k9l0m1n2
Create Date: 2026-08-07 10:23:44.423505

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '39681ca62c54'
down_revision: Union[str, None] = ('e4f7a9b1c3d5', 'i7j8k9l0m1n2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
