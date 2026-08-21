"""merge weather_cache and cutover_seed heads

Revision ID: b0cc0be78aba
Revises: cut0seedfdcn1, wthr01cache1
Create Date: 2026-08-21 11:33:26.542267

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b0cc0be78aba'
down_revision: Union[str, None] = ('cut0seedfdcn1', 'wthr01cache1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
