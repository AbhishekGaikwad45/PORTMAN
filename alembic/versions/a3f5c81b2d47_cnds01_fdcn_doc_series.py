"""CNDS01: DN/CN doc series table, with stale defaults neutralised

The fdcn_doc_series table outlived the code that read it (22857cc), so live
databases still hold rows flagged is_default from before — e.g. prefix
'DPPL/CN/26-27/'. Simply reading the table again would silently switch the CN
prefix away from DPPLCN mid-financial-year and restart the sequence.

So: create the table if it is missing, and clear any surviving default flags.
Prefixes and names are preserved and stay visible in CNDS01, where an admin can
deliberately re-enable one. Until they do, FDCN01 keeps issuing DPPLCN/DPPLDN
exactly as it does today — this migration changes no document number.

Revision ID: a3f5c81b2d47
Revises: cf01aa22bb33
Create Date: 2026-07-30
"""
from alembic import op

revision = 'a3f5c81b2d47'
down_revision = 'cf01aa22bb33'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS fdcn_doc_series (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            prefix      TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'CN',
            is_default  BOOLEAN DEFAULT FALSE
        )
    """)
    # Pre-existing installs may lack the columns the module expects.
    op.execute("ALTER TABLE fdcn_doc_series ADD COLUMN IF NOT EXISTS type TEXT NOT NULL DEFAULT 'CN'")
    op.execute("ALTER TABLE fdcn_doc_series ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT FALSE")
    # Stand down the orphaned defaults so numbering does not move on deploy.
    op.execute("UPDATE fdcn_doc_series SET is_default = FALSE WHERE is_default = TRUE")


def downgrade():
    # The table predates this revision; dropping it would destroy series config.
    pass
