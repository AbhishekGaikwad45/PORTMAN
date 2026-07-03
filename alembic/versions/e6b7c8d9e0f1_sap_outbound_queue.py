"""SAP outbound queue — async posting with retry

Backs the queue that decouples invoice generation / cancellation from the
synchronous SAP call. Mirrors the proven mail_queue pattern: status +
retry_count + max_retries, with a per-item next_attempt_at so the worker can
space retries 5 minutes apart. The full SAP payload is saved so a posting
survives SAP being down (e.g. Thursday migration windows) and can be retried
automatically or sent manually later.

Revision ID: e6b7c8d9e0f1
Revises: d5a6b7c8d9e0
Create Date: 2026-06-26
"""
from typing import Sequence, Union
from alembic import op


revision: str = 'e6b7c8d9e0f1'
down_revision: Union[str, None] = 'd5a6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS sap_outbound_queue (
            id                  SERIAL PRIMARY KEY,
            job_type            TEXT NOT NULL,          -- post | reversal | credit_note
            invoice_id          INTEGER,
            reference_type      TEXT,                   -- passed to sap_client (Invoice / InvoiceReversal / InvoiceCreditNote)
            reference_id        INTEGER,
            reference_number    TEXT,
            payload             TEXT NOT NULL,          -- saved SAP JSON
            status              TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | sent | failed
            retry_count         INTEGER NOT NULL DEFAULT 0,
            max_retries         INTEGER NOT NULL DEFAULT 10,
            next_attempt_at     TIMESTAMP,
            sap_document_number TEXT,
            last_error          TEXT,
            created_by          TEXT,
            created_date        TIMESTAMP,
            updated_date        TIMESTAMP
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_sap_queue_due
            ON sap_outbound_queue (status, next_attempt_at)
    """)
    # Dedupe guard: at most one active (not sent) job per invoice+job_type,
    # so double-clicks / double-enqueues can't double-post.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_sap_queue_active_job
            ON sap_outbound_queue (invoice_id, job_type)
            WHERE status <> 'sent' AND invoice_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS sap_outbound_queue')
