"""Guards the ?with_billables=1 customer list on FIN01 Generate Bill.

Read-only against whatever database config points at. Fails if the counting SQL
stops parsing, starts returning zero-count parties, or loses its ordering.

    python -m pytest test_billing_customer_filter.py -q
"""
from database import get_db, get_cursor
from modules.FIN01.views import _BILLABLE_CARGO_COUNT

COLS = ('id, name, gstin, gst_state_code, billing_address, city, pincode, '
        'contact_phone, contact_email')

PARTIES = [('Customer', 'vessel_customers', ''),
           ('Agent', 'vessel_agents', 'WHERE is_active = 1')]


def _counted(cur, customer_type, table, active):
    cur.execute(f'''
        WITH cargo AS ({_BILLABLE_CARGO_COUNT}),
        svc AS (
            SELECT source_id AS id, COUNT(*) AS n
            FROM service_records
            WHERE source_type = %s AND doc_status = 'Approved'
              AND COALESCE(is_billed, 0) = 0
            GROUP BY source_id
        )
        SELECT p.{COLS.replace(', ', ', p.')},
               COALESCE((SELECT SUM(n) FROM cargo c WHERE c.name = p.name), 0)
             + COALESCE((SELECT n FROM svc s WHERE s.id = p.id), 0) AS billable_count
        FROM {table} p
        {active}
        ORDER BY billable_count DESC, p.name
    ''', [customer_type])
    return [dict(r) for r in cur.fetchall()]


def test_with_billables_filters_and_orders():
    conn = get_db()
    cur = get_cursor(conn)
    try:
        for customer_type, table, active in PARTIES:
            rows = _counted(cur, customer_type, table, active)
            kept = [r for r in rows if r['billable_count'] > 0]

            # what the endpoint ships: only parties with something to bill
            assert all(r['billable_count'] > 0 for r in kept)

            # busiest first -- the auto-select picks kept[0]
            counts = [r['billable_count'] for r in kept]
            assert counts == sorted(counts, reverse=True), \
                f'{customer_type} not ordered by billable_count: {counts}'

            # filtering never invents parties
            cur.execute(f'SELECT COUNT(*) AS n FROM {table} {active}')
            assert len(kept) <= cur.fetchone()['n']
    finally:
        conn.close()


MBC_UNBILLED = '''
    FROM mbc_customer_details cd
    JOIN mbc_header mh ON mh.id = cd.mbc_id
    WHERE (COALESCE(cd.is_billed, 0) = 0
           OR COALESCE(cd.billed_quantity, 0) < cd.quantity)
'''


def test_draft_mbc_lines_are_excluded_from_the_count():
    """A Draft MBC renders locked in the UI, so it must not make its customer
    show up as having billable items. gated MBC count == unbilled - drafts."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        # the MBC arm of the counting SQL, isolated
        cur.execute(f'''
            SELECT COUNT(*) AS n {MBC_UNBILLED}
              AND mh.doc_status IN ('Approved', 'Closed', 'Partial Close')
        ''')
        gated = cur.fetchone()['n']

        cur.execute(f'SELECT COUNT(*) AS n {MBC_UNBILLED}')
        unbilled = cur.fetchone()['n']

        cur.execute(f'''
            SELECT COUNT(*) AS n {MBC_UNBILLED}
              AND mh.doc_status NOT IN ('Approved', 'Closed', 'Partial Close')
        ''')
        drafts = cur.fetchone()['n']

        assert gated == unbilled - drafts, \
            f'gate leaks: gated={gated} unbilled={unbilled} drafts={drafts}'
    finally:
        conn.close()
