"""weather_cache: JSONB cache for WeatherAPI responses

Revision ID: wthr01cache1
Revises: 39681ca62c54
Create Date: 2026-08-12
"""
from typing import Sequence, Union
from alembic import op

revision: str = 'wthr01cache1'
down_revision: Union[str, None] = '39681ca62c54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS weather_cache (
            cache_key  TEXT PRIMARY KEY,
            data       JSONB NOT NULL,
            fetched_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS weather_cache")
