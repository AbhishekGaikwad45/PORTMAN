from database import get_db, get_cursor
from datetime import datetime


# ===== CUTOVER SEED HELPERS =====

def would_overbill(billed_qty, add_qty, total_qty):
    """True if billing add_qty on top of billed_qty exceeds the source's total
    billable quantity (small tolerance absorbs float noise). Used to block a
    stale page / double-submit from billing the same cargo twice while still
    allowing legitimate partial billing up to the full quantity."""
    return float(billed_qty or 0) + float(add_qty or 0) > float(total_qty or 0) + 0.001


def next_from_seed(existing_max, start_seq):
    """Next sequence number given the highest already-used number and an
    optional cutover floor. The seed is a floor only: once real documents
    exceed it, normal incrementing wins, so a stale seed can never collide."""
    base = (existing_max or 0) + 1
    if start_seq:
        return max(base, int(start_seq))
    return base


def lookup_seed(cur, seed_type, doc_series='', financial_year=''):
    """Return the cutover start_seq for this key, or None. Tolerates a missing
    table (pre-migration) by returning None."""
    try:
        cur.execute(
            '''SELECT start_seq FROM cutover_seed
               WHERE seed_type=%s AND doc_series=%s AND financial_year=%s''',
            [seed_type, doc_series or '', financial_year or ''])
        row = cur.fetchone()
        return row['start_seq'] if row else None
    except Exception:
        cur.connection.rollback()
        return None


def next_invoice_seq(cur, doc_series, financial_year):
    """Next invoice doc_series_seq for (doc_series, fy), honouring a cutover seed
    as a floor. Uses the SAME key as the existing MAX query."""
    cur.execute(
        'SELECT MAX(doc_series_seq) AS m FROM invoice_header WHERE doc_series=%s AND financial_year=%s',
        [doc_series, financial_year])
    row = cur.fetchone()
    existing_max = (row['m'] if row else 0) or 0
    seed = lookup_seed(cur, 'invoice', doc_series, financial_year)
    return next_from_seed(existing_max, seed)


# ===== CARGO BILLING HELPERS =====

_CARGO_TABLES = {
    'VCN_IMPORT': ('vcn_cargo_declaration', 'bl_quantity'),
    'VCN_EXPORT': ('vcn_export_cargo_declaration', 'bl_quantity'),
    'MBC':        ('mbc_customer_details', 'quantity'),
}

def _mark_cargo_source_billed(cur, cargo_source_type, cargo_source_id, bill_qty, bill_id):
    """Increment billed_quantity on the correct declaration row."""
    if not cargo_source_type or not cargo_source_id:
        return
    bill_qty = float(bill_qty or 0)
    if cargo_source_type == 'VCN_IMPORT':
        cur.execute('''
            UPDATE vcn_cargo_declaration
            SET billed_quantity = COALESCE(billed_quantity, 0) + %s,
                bill_id = %s,
                is_billed = CASE
                    WHEN COALESCE(billed_quantity, 0) + %s >= bl_quantity THEN 1
                    ELSE 0
                END
            WHERE id = %s
        ''', [bill_qty, bill_id, bill_qty, cargo_source_id])
    elif cargo_source_type == 'VCN_EXPORT':
        cur.execute('''
            UPDATE vcn_export_cargo_declaration
            SET billed_quantity = COALESCE(billed_quantity, 0) + %s,
                bill_id = %s,
                is_billed = CASE
                    WHEN COALESCE(billed_quantity, 0) + %s >= bl_quantity THEN 1
                    ELSE 0
                END
            WHERE id = %s
        ''', [bill_qty, bill_id, bill_qty, cargo_source_id])
    elif cargo_source_type == 'MBC':
        cur.execute('''
            UPDATE mbc_customer_details
            SET billed_quantity = COALESCE(billed_quantity, 0) + %s,
                bill_id = %s,
                is_billed = CASE
                    WHEN COALESCE(billed_quantity, 0) + %s >= quantity THEN 1
                    ELSE 0
                END
            WHERE id = %s
        ''', [bill_qty, bill_id, bill_qty, cargo_source_id])


def _unmark_cargo_source_billed(cur, cargo_source_type, cargo_source_id, bill_qty):
    """Decrement billed_quantity on the correct declaration row (bill delete/reversal)."""
    if not cargo_source_type or not cargo_source_id:
        return
    bill_qty = float(bill_qty or 0)
    entry = _CARGO_TABLES.get(cargo_source_type)
    if not entry:
        return
    table, total_col = entry
    cur.execute(f'''
        UPDATE {table}
        SET billed_quantity = GREATEST(COALESCE(billed_quantity, 0) - %s, 0),
            is_billed = CASE
                WHEN GREATEST(COALESCE(billed_quantity, 0) - %s, 0) >= COALESCE({total_col}, 0)
                     AND COALESCE({total_col}, 0) > 0 THEN 1
                ELSE 0
            END,
            bill_id = CASE
                WHEN GREATEST(COALESCE(billed_quantity, 0) - %s, 0) <= 0 THEN NULL
                ELSE bill_id
            END
        WHERE id = %s
    ''', [bill_qty, bill_qty, bill_qty, cargo_source_id])



def _mark_service_record_billed(cur, service_record_id, bill_qty, bill_id):
    """Increment billed_quantity on a service record (partial billing)."""
    if not service_record_id:
        return
    bill_qty = float(bill_qty or 0)
    cur.execute("""
        UPDATE service_records
        SET billed_quantity = COALESCE(billed_quantity, 0) + %s,
            bill_id = %s,
            is_billed = CASE
                WHEN COALESCE(billed_quantity, 0) + %s >= COALESCE(billable_quantity, 0)
                     AND COALESCE(billable_quantity, 0) > 0 THEN 1
                ELSE 0
            END
        WHERE id = %s
    """, [bill_qty, bill_id, bill_qty, service_record_id])


def _unmark_service_record_billed(cur, service_record_id, bill_qty):
    """Decrement billed_quantity on a service record (bill delete/reversal)."""
    if not service_record_id:
        return
    bill_qty = float(bill_qty or 0)
    cur.execute("""
        UPDATE service_records
        SET billed_quantity = GREATEST(COALESCE(billed_quantity, 0) - %s, 0),
            is_billed = CASE
                WHEN GREATEST(COALESCE(billed_quantity, 0) - %s, 0) >= COALESCE(billable_quantity, 0)
                     AND COALESCE(billable_quantity, 0) > 0 THEN 1
                ELSE 0
            END,
            bill_id = CASE
                WHEN GREATEST(COALESCE(billed_quantity, 0) - %s, 0) <= 0 THEN NULL
                ELSE bill_id
            END
        WHERE id = %s
    """, [bill_qty, bill_qty, bill_qty, service_record_id])

# ===== PARTY GUARDS (one invoice = one party) =====

def _norm_party(name):
    """Collapse a party name for comparison: trimmed, single-spaced, casefolded."""
    return ' '.join(str(name or '').split()).casefold()


def same_party(bill_customer, cargo_customer):
    """Do a bill's customer and a cargo declaration's customer refer to one party?

    Names are the join key everywhere else in billing (get_customer_billables
    filters declarations by customer_name), so they are the key here too.
    A blank declaration customer stakes no claim and never blocks — only a
    populated, genuinely different name does."""
    cargo = _norm_party(cargo_customer)
    if not cargo:
        return True
    return cargo == _norm_party(bill_customer)


def single_party(bill_rows):
    """True if every bill row belongs to the same (customer_type, customer_id).
    An empty selection is vacuously single-party; the caller rejects it earlier."""
    return len({(r['customer_type'], r['customer_id']) for r in bill_rows}) <= 1


def get_bill_parties(cur, bill_ids):
    """The distinct parties across the given bills, for the single_party check."""
    cur.execute('''SELECT DISTINCT customer_type, customer_id, customer_name
                   FROM bill_header WHERE id = ANY(%s)''', [list(bill_ids)])
    return [dict(r) for r in cur.fetchall()]


def find_cargo_party_mismatch(cur, bill_ids):
    """Cargo lines whose declaration no longer belongs to its bill's customer.

    This is the guard that catches a declaration retro-edited after billing:
    at bill time the names matched (that is how the item got listed), but if
    someone later retypes customer_name on the MBC/VCN row, the bill silently
    starts billing another party's cargo. Checked at invoice time — the point
    where the money leaves — so the drift can never reach a customer.

    Returns a list of {bill_number, bill_customer, cargo_customer, cargo_name}."""
    problems = []
    for cargo_source_type, (table, _qty_col) in _CARGO_TABLES.items():
        # table comes from the _CARGO_TABLES constant, never from user input.
        cur.execute(f'''
            SELECT bh.bill_number, bh.customer_name AS bill_customer,
                   d.customer_name AS cargo_customer, d.cargo_name
            FROM bill_lines bl
            JOIN bill_header bh ON bh.id = bl.bill_id
            JOIN {table} d ON d.id = bl.cargo_source_id
            WHERE bl.bill_id = ANY(%s) AND bl.cargo_source_type = %s
        ''', [list(bill_ids), cargo_source_type])
        for row in cur.fetchall():
            r = dict(row)
            if not same_party(r['bill_customer'], r['cargo_customer']):
                problems.append(r)
    return problems


# ===== BILLING LOCK (an operational doc freezes once its cargo is billed) =====

# Which declaration table + owning-document column each source document uses.
_SOURCE_DECLARATIONS = {
    'MBC': [('mbc_customer_details',          'mbc_id', 'MBC')],
    'VCN': [('vcn_cargo_declaration',         'vcn_id', 'VCN_IMPORT'),
            ('vcn_export_cargo_declaration',  'vcn_id', 'VCN_EXPORT')],
}

# A bill in these states no longer holds its cargo — rejection and cancellation
# both release the billed tracking, so the source document must open up again.
_DEAD_BILL_STATUSES = ('Cancelled', 'Rejected')


def source_lock_state(cur, source_kind, source_id):
    """Billing lock on an operational document (MBC doc or VCN call).

    Returns:
      ''          nothing billed — freely editable
      'billed'    cargo sits on a live bill; frozen, but rejecting or removing
                  that bill releases the cargo and reopens the document
      'invoiced'  cargo reached an invoice; permanent, never editable again

    'invoiced' deliberately ignores bill status: once a customer has been
    invoiced off this cargo, the operational record behind it is evidence and
    must never move, even if the bill is later cancelled.

    source_kind: 'MBC' or 'VCN'."""
    tables = _SOURCE_DECLARATIONS.get(source_kind)
    if not tables or not source_id:
        return ''

    # Table/column names come from the _SOURCE_DECLARATIONS constant, never input.
    for table, owner_col, cargo_source_type in tables:
        cur.execute(f'''
            SELECT 1 FROM {table} d
            JOIN bill_lines bl ON bl.cargo_source_type = %s AND bl.cargo_source_id = d.id
            JOIN invoice_bill_mapping ibm ON ibm.bill_id = bl.bill_id
            WHERE d.{owner_col} = %s LIMIT 1
        ''', [cargo_source_type, source_id])
        if cur.fetchone():
            return 'invoiced'

    for table, owner_col, cargo_source_type in tables:
        cur.execute(f'''
            SELECT 1 FROM {table} d
            LEFT JOIN bill_lines bl ON bl.cargo_source_type = %s AND bl.cargo_source_id = d.id
            LEFT JOIN bill_header bh ON bh.id = bl.bill_id
            WHERE d.{owner_col} = %s
              AND (
                    (bl.id IS NOT NULL AND COALESCE(bh.bill_status, '') <> ALL(%s))
                    -- cutover-marked cargo carries no bill row but is still billed
                    OR COALESCE(d.is_billed, 0) = 1
                    OR COALESCE(d.billed_quantity, 0) > 0
                  )
            LIMIT 1
        ''', [cargo_source_type, source_id, list(_DEAD_BILL_STATUSES)])
        if cur.fetchone():
            return 'billed'

    return ''


def lock_message(state, doc_kind='document'):
    """User-facing reason a locked document cannot be changed, or '' if open."""
    if state == 'invoiced':
        return (f'This {doc_kind} has been invoiced. Invoiced records are permanent '
                f'and can never be edited, reopened or deleted.')
    if state == 'billed':
        return (f'This {doc_kind} has been billed. Remove or reject the bill first '
                f'(Admin → Remove Bill) — that releases the cargo and reopens it.')
    return ''


def release_bill_sources(cur, bill_id, only_if_still_linked=False):
    """Reverse cargo + service-record billed tracking held by one bill
    (used on bill rejection and by the startup reconciliation).

    Clears the declaration's bill_id link afterwards so a second pass can
    never release the same quantities twice.

    only_if_still_linked: skip cargo lines whose declaration no longer points
    at this bill — keeps the startup reconciliation idempotent.
    Runs inside the caller's transaction — does NOT commit."""
    cur.execute('''
        SELECT cargo_source_type, cargo_source_id, quantity
        FROM bill_lines
        WHERE bill_id = %s AND cargo_source_type IS NOT NULL AND cargo_source_id IS NOT NULL
    ''', [bill_id])
    for row in cur.fetchall():
        entry = _CARGO_TABLES.get(row['cargo_source_type'])
        if not entry:
            continue
        table = entry[0]
        if only_if_still_linked:
            cur.execute(f'SELECT 1 FROM {table} WHERE id = %s AND bill_id = %s',
                        [row['cargo_source_id'], bill_id])
            if not cur.fetchone():
                continue
        _unmark_cargo_source_billed(
            cur,
            row['cargo_source_type'],
            row['cargo_source_id'],
            float(row['quantity'] or 0)
        )
        cur.execute(f'UPDATE {table} SET bill_id = NULL WHERE id = %s AND bill_id = %s',
                    [row['cargo_source_id'], bill_id])
    cur.execute('''
        SELECT service_record_id, SUM(quantity) AS q
        FROM bill_lines
        WHERE bill_id = %s AND service_record_id IS NOT NULL
        GROUP BY service_record_id
    ''', [bill_id])
    for row in [dict(r) for r in cur.fetchall()]:
        srid = row['service_record_id']
        if only_if_still_linked:
            cur.execute('SELECT 1 FROM service_records WHERE id = %s AND bill_id = %s',
                        [srid, bill_id])
            if not cur.fetchone():
                continue
        _unmark_service_record_billed(cur, srid, float(row['q'] or 0))


def reapply_bill_sources(cur, bill_id):
    """Re-mark cargo + service records as billed when a rejected bill is
    resubmitted for approval. Mirror image of release_bill_sources.
    Runs inside the caller's transaction — does NOT commit."""
    cur.execute('''
        SELECT cargo_source_type, cargo_source_id, quantity, service_record_id
        FROM bill_lines
        WHERE bill_id = %s
    ''', [bill_id])
    for row in cur.fetchall():
        _mark_cargo_source_billed(
            cur,
            row['cargo_source_type'],
            row['cargo_source_id'],
            float(row['quantity'] or 0),
            bill_id
        )
        if row.get('service_record_id'):
            _mark_service_record_billed(cur, row['service_record_id'],
                                        float(row['quantity'] or 0), bill_id)


def reconcile_billed_tracking(cur=None):
    """Startup self-heal: rebuild billed tracking from the bills that exist.

    billed_quantity / is_billed / bill_id are stored counters on the cargo
    declaration, so every path that ends a bill has to reverse them by hand —
    and anything that ever failed to (a rejection from a build that predates
    release_bill_sources, a bill deleted straight in SQL) leaves that cargo
    stuck as non-billable with nothing to un-stick it. Recompute the counters
    from the bill lines that actually sit on live (not rejected, not cancelled)
    bills, which is what the counters are supposed to mean.

    Only declarations that appear in bill_lines at all are touched, so
    cutover-marked cargo (is_billed=1, no bill row) keeps its flag. Idempotent.
    Pass a cursor to join the caller's transaction.
    Returns {table: rows_repaired}."""
    conn = None if cur is not None else get_db()
    if conn is not None:
        cur = get_cursor(conn)
    dead = list(_DEAD_BILL_STATUSES)
    repaired = {}
    for cargo_source_type, (table, total_col) in _CARGO_TABLES.items():
        # Table/column names come from the _CARGO_TABLES constant, never input.
        cur.execute(f'''
            UPDATE {table} d
            SET billed_quantity = live.q,
                is_billed = CASE WHEN live.q >= COALESCE(d.{total_col}, 0)
                                  AND COALESCE(d.{total_col}, 0) > 0 THEN 1 ELSE 0 END,
                bill_id = live.bill_id
            FROM (
                SELECT bl.cargo_source_id AS id,
                       COALESCE(SUM(CASE WHEN bh.bill_status <> ALL(%s)
                                         THEN bl.quantity ELSE 0 END), 0) AS q,
                       MAX(CASE WHEN bh.bill_status <> ALL(%s)
                                THEN bl.bill_id END) AS bill_id
                FROM bill_lines bl
                JOIN bill_header bh ON bh.id = bl.bill_id
                WHERE bl.cargo_source_type = %s AND bl.cargo_source_id IS NOT NULL
                GROUP BY bl.cargo_source_id
            ) live
            WHERE d.id = live.id
              -- bill_id IS NULL + flagged billed is the admin cutover tool's own
              -- signature (it never sets bill_id), and stale tracking looks
              -- identical. Never guess: those are reported, not repaired.
              AND d.bill_id IS NOT NULL
              AND (COALESCE(d.billed_quantity, 0) <> live.q
                   OR COALESCE(d.is_billed, 0) <> CASE WHEN live.q >= COALESCE(d.{total_col}, 0)
                                                        AND COALESCE(d.{total_col}, 0) > 0
                                                       THEN 1 ELSE 0 END)
        ''', [dead, dead, cargo_source_type])
        if cur.rowcount:
            repaired[table] = cur.rowcount

    # Service records carry the same counters (partial billing), same recompute.
    cur.execute('''
        UPDATE service_records sr
        SET billed_quantity = live.q,
            is_billed = CASE WHEN live.q >= COALESCE(sr.billable_quantity, 0)
                              AND COALESCE(sr.billable_quantity, 0) > 0 THEN 1 ELSE 0 END,
            bill_id = live.bill_id
        FROM (
            SELECT bl.service_record_id AS id,
                   COALESCE(SUM(CASE WHEN bh.bill_status <> ALL(%s)
                                     THEN bl.quantity ELSE 0 END), 0) AS q,
                   MAX(CASE WHEN bh.bill_status <> ALL(%s)
                            THEN bl.bill_id END) AS bill_id
            FROM bill_lines bl
            JOIN bill_header bh ON bh.id = bl.bill_id
            WHERE bl.service_record_id IS NOT NULL
            GROUP BY bl.service_record_id
        ) live
        WHERE sr.id = live.id
          -- same rule as cargo: bill_id IS NULL + flagged billed is the admin
          -- cutover signature, never guessed at here.
          AND sr.bill_id IS NOT NULL
          AND (COALESCE(sr.billed_quantity, 0) <> live.q
               OR COALESCE(sr.is_billed, 0) <> CASE WHEN live.q >= COALESCE(sr.billable_quantity, 0)
                                                     AND COALESCE(sr.billable_quantity, 0) > 0
                                                    THEN 1 ELSE 0 END)
    ''', [dead, dead])
    if cur.rowcount:
        repaired['service_records'] = cur.rowcount

    if conn is not None:
        conn.commit()
        conn.close()
    return repaired


def stuck_billed_cargo(cur=None):
    """Cargo flagged billed with no live bill behind it and no bill_id link.

    Two different things land here and the row itself cannot tell them apart:
    a cutover mark (Admin → Cutover marks is_billed without ever setting
    bill_id — the cargo really was billed in the old system) and tracking left
    over from a bill that was rejected or cancelled without releasing it. So
    these are never auto-repaired: cutover_audit says which is which, and
    Admin → Cutover → unmark frees the ones that are genuinely stale.

    Returns [{source_type, id, customer_name, total_quantity, billed_quantity}]."""
    conn = None if cur is not None else get_db()
    if conn is not None:
        cur = get_cursor(conn)
    dead = list(_DEAD_BILL_STATUSES)
    rows = []
    for cargo_source_type, (table, total_col) in _CARGO_TABLES.items():
        # Table/column names come from the _CARGO_TABLES constant, never input.
        cur.execute(f'''
            SELECT d.id, d.customer_name, d.{total_col} AS total_quantity, d.billed_quantity
            FROM {table} d
            WHERE d.bill_id IS NULL
              AND (COALESCE(d.is_billed, 0) = 1 OR COALESCE(d.billed_quantity, 0) > 0)
              AND NOT EXISTS (SELECT 1 FROM bill_lines bl
                              JOIN bill_header bh ON bh.id = bl.bill_id
                              WHERE bl.cargo_source_type = %s AND bl.cargo_source_id = d.id
                                AND bh.bill_status <> ALL(%s))
            ORDER BY d.id
        ''', [cargo_source_type, dead])
        for r in cur.fetchall():
            rows.append({'source_type': cargo_source_type, **dict(r)})
    if conn is not None:
        conn.close()
    return rows


def unbill_invoice_sources(cur, invoice_id):
    """Fully unbill everything behind a cancelled invoice, down to cargo level.

    For each bill linked via invoice_bill_mapping: reverse cargo declaration
    tracking (billed_quantity / is_billed), unmark service records, and set the
    bill itself to 'Cancelled' (kept for audit, not deleted) so the cargo can be
    re-billed and re-invoiced under a fresh invoice number.

    Runs inside the caller's transaction — takes a cursor and does NOT commit.
    Returns the list of affected bill numbers for the cancellation remark."""
    cur.execute('''SELECT ibm.bill_id, ibm.bill_number
        FROM invoice_bill_mapping ibm
        WHERE ibm.invoice_id = %s''', [invoice_id])
    bills = [dict(r) for r in cur.fetchall()]

    for bill in bills:
        bill_id = bill['bill_id']
        cur.execute('''
            SELECT cargo_source_type, cargo_source_id, quantity
            FROM bill_lines
            WHERE bill_id = %s AND cargo_source_type IS NOT NULL AND cargo_source_id IS NOT NULL
        ''', [bill_id])
        for row in cur.fetchall():
            _unmark_cargo_source_billed(
                cur,
                row['cargo_source_type'],
                row['cargo_source_id'],
                float(row['quantity'] or 0)
            )
        cur.execute('''SELECT service_record_id, SUM(quantity) AS q FROM bill_lines
            WHERE bill_id = %s AND service_record_id IS NOT NULL
            GROUP BY service_record_id''', [bill_id])
        for row in [dict(r) for r in cur.fetchall()]:
            _unmark_service_record_billed(cur, row['service_record_id'], float(row['q'] or 0))
        cur.execute("UPDATE bill_header SET bill_status='Cancelled' WHERE id=%s", [bill_id])

    return [b['bill_number'] for b in bills]


# ===== BILL FUNCTIONS =====

def get_next_bill_number(cur=None):
    """Generate next bill number, honouring a cutover bill seed as a floor.

    Pass the caller's cursor to read inside its transaction."""
    conn = None if cur is not None else get_db()
    if conn is not None:
        cur = get_cursor(conn)
    cur.execute(
        "SELECT MAX(CAST(SUBSTR(bill_number, 5) AS INTEGER)) FROM bill_header WHERE bill_number LIKE 'BILL%%'"
    )
    existing_max = (cur.fetchone()['max'] or 0)
    seed = lookup_seed(cur, 'bill')      # doc_series='', financial_year=''
    if conn is not None:
        conn.close()
    next_num = next_from_seed(existing_max, seed)
    return f"BILL{next_num:04d}"


def get_bill_data(page=1, size=20, status_filter=None):
    """Get paginated bills"""
    conn = get_db()
    cur = get_cursor(conn)

    where_clause = ""
    params = []
    if status_filter:
        where_clause = "WHERE b.bill_status = %s"
        params.append(status_filter)

    cur.execute(f'SELECT COUNT(*) FROM bill_header b {where_clause}', params)
    total = cur.fetchone()['count']
    cur.execute(f'''
        SELECT
            b.*,
            ca.agreement_code,
            ca.agreement_name,
            NULLIF(
                TRIM(
                    COALESCE(ca.agreement_code, '') ||
                    CASE
                        WHEN COALESCE(ca.agreement_name, '') <> '' THEN ' - ' || ca.agreement_name
                        ELSE ''
                    END
                ),
                ''
            ) AS agreement_display
        FROM bill_header b
        LEFT JOIN customer_agreements ca ON b.agreement_id = ca.id
        {where_clause}
        ORDER BY b.id DESC
        LIMIT %s OFFSET %s
    ''', params + [size, (page-1)*size])
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def save_bill_header(data, cur=None):
    """Save bill header. Pass the caller's cursor to join its transaction
    (nothing is committed then — the caller owns commit/rollback)."""
    conn = None if cur is not None else get_db()
    if conn is not None:
        cur = get_cursor(conn)
    row_id = data.get('id')

    if row_id:
        cols = [k for k in data if k not in ['id', 'bill_number']]
        cur.execute(f'''UPDATE bill_header
            SET {', '.join([f'{c}=%s' for c in cols])}
            WHERE id=%s''',
            [data[c] for c in cols] + [row_id])
    else:
        data['bill_number'] = get_next_bill_number(cur)
        cols = [k for k in data if k != 'id']
        cur.execute(f'''INSERT INTO bill_header
            ({', '.join(cols)})
            VALUES ({', '.join(['%s']*len(cols))})
            RETURNING id''',
            [data[c] for c in cols])
        row_id = cur.fetchone()['id']

    if conn is not None:
        conn.commit()
        conn.close()
    return row_id, data.get('bill_number')


def _other_active_bill_for_cargo(cur, cargo_source_type, cargo_source_id, exclude_bill_id):
    """A surviving (non-cancelled) bill other than exclude_bill_id that still
    bills this cargo source, or None."""
    cur.execute('''SELECT bl.bill_id FROM bill_lines bl
                   JOIN bill_header bh ON bh.id = bl.bill_id
                   WHERE bl.cargo_source_type=%s AND bl.cargo_source_id=%s
                     AND bl.bill_id <> %s AND bh.bill_status <> 'Cancelled'
                   ORDER BY bl.bill_id LIMIT 1''',
                [cargo_source_type, cargo_source_id, exclude_bill_id])
    r = cur.fetchone()
    return r['bill_id'] if r else None


def _other_active_bill_for_service(cur, service_record_id, exclude_bill_id):
    """A surviving (non-cancelled) bill other than exclude_bill_id that still
    bills this service record, or None."""
    cur.execute('''SELECT bl.bill_id FROM bill_lines bl
                   JOIN bill_header bh ON bh.id = bl.bill_id
                   WHERE bl.service_record_id=%s AND bl.bill_id <> %s
                     AND bh.bill_status <> 'Cancelled'
                   ORDER BY bl.bill_id LIMIT 1''',
                [service_record_id, exclude_bill_id])
    r = cur.fetchone()
    return r['bill_id'] if r else None


def _release_bill_for_delete(cur, bill_id):
    """Release the billed tracking a bill holds when DELETING it — share-aware.

    Undoes only THIS bill's contribution. If another active (non-cancelled)
    bill also bills the same source (e.g. a duplicate of an already-invoiced
    bill), that source keeps its billed state and ownership is handed to the
    surviving bill — so removing the duplicate never un-bills items the other
    bill still owns. Runs inside the caller's transaction; does NOT commit."""
    cur.execute('''SELECT cargo_source_type, cargo_source_id, quantity
                   FROM bill_lines
                   WHERE bill_id=%s AND cargo_source_type IS NOT NULL
                     AND cargo_source_id IS NOT NULL''', [bill_id])
    for row in [dict(r) for r in cur.fetchall()]:
        cstype, csid = row['cargo_source_type'], row['cargo_source_id']
        entry = _CARGO_TABLES.get(cstype)
        if not entry:
            continue
        table = entry[0]
        # Undo this bill's quantity contribution (recomputes billed_quantity + is_billed).
        _unmark_cargo_source_billed(cur, cstype, csid, float(row['quantity'] or 0))
        # Hand the declaration's bill_id to a surviving bill, else clear it.
        other = _other_active_bill_for_cargo(cur, cstype, csid, bill_id)
        cur.execute(f'UPDATE {table} SET bill_id=%s WHERE id=%s', [other, csid])

    cur.execute('''SELECT service_record_id, SUM(quantity) AS q FROM bill_lines
                   WHERE bill_id=%s AND service_record_id IS NOT NULL
                   GROUP BY service_record_id''', [bill_id])
    for row in [dict(r) for r in cur.fetchall()]:
        srid = row['service_record_id']
        # Undo this bill's quantity contribution, then hand ownership over.
        _unmark_service_record_billed(cur, srid, float(row['q'] or 0))
        other = _other_active_bill_for_service(cur, srid, bill_id)
        if other:
            cur.execute('UPDATE service_records SET bill_id=%s WHERE id=%s', [other, srid])


# ===== CARGO SWAP (replace wrong cargo on a bill, money untouched) =====

_MONEY_FIELDS = ('line_amount', 'cgst_amount', 'sgst_amount', 'igst_amount',
                 'line_total', 'tds_amount', 'tcs_amount')


def split_line_amounts(original, quantities):
    """Split one bill line's money across replacement quantities.

    Each share is prorated by quantity and the residual paisa is pushed onto the
    largest share, so the parts sum EXACTLY to the original — never a rupee more
    or less. That exactness is what allows a swap on an already-invoiced bill:
    the bill total, the invoice total and the SAP posting all stay put, so only
    which cargo backs the charge changes.

    original:   dict carrying the _MONEY_FIELDS (missing keys count as 0)
    quantities: replacement quantities; their sum must equal the original
                quantity (the caller enforces that)
    Returns one dict of money fields per quantity."""
    quantities = [float(q or 0) for q in quantities]
    total = sum(quantities)
    if not quantities or total <= 0:
        raise ValueError('Replacement quantities must be positive')

    biggest = max(range(len(quantities)), key=lambda i: quantities[i])
    shares = [{} for _ in quantities]
    for field in _MONEY_FIELDS:
        source = round(float(original.get(field) or 0), 2)
        parts = [round(source * q / total, 2) for q in quantities]
        residual = round(source - sum(parts), 2)
        if residual:
            parts[biggest] = round(parts[biggest] + residual, 2)
        for share, value in zip(shares, parts):
            share[field] = value
    return shares


def quantities_balance(original_quantity, quantities):
    """Do the replacement quantities add up to exactly what is being removed?
    Same 0.001 tolerance as would_overbill, to absorb float noise."""
    return abs(sum(float(q or 0) for q in quantities)
               - float(original_quantity or 0)) <= 0.001


# ===== CARGO RELEASE (admin escape hatch for mis-billed cargo) =====

# (cargo_source_type, declaration table, owner column, parent table, doc column,
#  vessel column, total-quantity column)
_RELEASE_SOURCES = (
    ('MBC',        'mbc_customer_details',         'mbc_id', 'mbc_header', 'doc_num',     'mbc_name',    'quantity'),
    ('VCN_IMPORT', 'vcn_cargo_declaration',        'vcn_id', 'vcn_header', 'vcn_doc_num', 'vessel_name', 'bl_quantity'),
    ('VCN_EXPORT', 'vcn_export_cargo_declaration', 'vcn_id', 'vcn_header', 'vcn_doc_num', 'vessel_name', 'bl_quantity'),
)


def get_billed_cargo(search='', limit=200):
    """Cargo currently marked billed, with whoever is holding it.

    Feeds the admin Release Cargo tab — the escape hatch for cargo billed to the
    wrong party, where Uninvoice and Remove Bill are both refused because the
    invoice reached SAP. One row per declaration; bills and invoices touching it
    are aggregated so the admin can see exactly what they are cutting loose."""
    term = f"%{(search or '').strip()}%"
    conn = get_db()
    cur = get_cursor(conn)
    rows = []
    try:
        for (cstype, table, owner_col, parent, doc_col, vessel_col, qty_col) in _RELEASE_SOURCES:
            # Every identifier here comes from the _RELEASE_SOURCES constant.
            cur.execute(f'''
                SELECT %s AS cargo_source_type, d.id AS cargo_source_id,
                       p.{doc_col}    AS doc_num,
                       p.{vessel_col} AS vessel_name,
                       d.customer_name, d.cargo_name,
                       COALESCE(d.{qty_col}, 0)       AS total_quantity,
                       COALESCE(d.billed_quantity, 0) AS billed_quantity,
                       COALESCE(STRING_AGG(DISTINCT bh.bill_number, ', '), '')    AS bills,
                       COALESCE(STRING_AGG(DISTINCT ih.invoice_number, ', '), '') AS invoices
                FROM {table} d
                JOIN {parent} p ON p.id = d.{owner_col}
                LEFT JOIN bill_lines bl
                       ON bl.cargo_source_type = %s AND bl.cargo_source_id = d.id
                LEFT JOIN bill_header bh ON bh.id = bl.bill_id
                LEFT JOIN invoice_bill_mapping ibm ON ibm.bill_id = bh.id
                LEFT JOIN invoice_header ih ON ih.id = ibm.invoice_id
                WHERE (COALESCE(d.is_billed, 0) = 1 OR COALESCE(d.billed_quantity, 0) > 0)
                  AND (COALESCE(p.{doc_col}, '')    ILIKE %s
                    OR COALESCE(p.{vessel_col}, '') ILIKE %s
                    OR COALESCE(d.customer_name, '') ILIKE %s
                    OR COALESCE(d.cargo_name, '')    ILIKE %s)
                GROUP BY d.id, p.{doc_col}, p.{vessel_col}, d.customer_name,
                         d.cargo_name, d.{qty_col}, d.billed_quantity
                ORDER BY d.id DESC
                LIMIT %s
            ''', [cstype, cstype, term, term, term, term, limit])
            rows.extend(dict(r) for r in cur.fetchall())
    finally:
        conn.close()
    rows.sort(key=lambda r: (r['doc_num'] or '', r['cargo_source_id']), reverse=True)
    return rows[:limit]


def release_cargo(cargo_source_type, cargo_source_id, username, reason=''):
    """Make one cargo declaration billable again, leaving every document intact.

    Clears only the billed tracking (is_billed / billed_quantity / bill_id) on
    that single declaration. Bills, invoices and SAP postings are deliberately
    untouched: when cargo has been invoiced to the wrong party the money is
    corrected by a credit note, not by rewriting an issued invoice. The freed
    cargo can then be billed to its rightful customer.

    Returns {'ok': bool, 'cargo', 'error'}."""
    entry = _CARGO_TABLES.get(cargo_source_type)
    if not entry:
        return {'ok': False, 'error': f'Unknown cargo source type: {cargo_source_type}'}
    table = entry[0]

    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute(f'SELECT customer_name, cargo_name, billed_quantity FROM {table} '
                    f'WHERE id=%s FOR UPDATE', [cargo_source_id])
        row = cur.fetchone()
        if not row:
            conn.close()
            return {'ok': False, 'error': 'Cargo declaration not found'}
        before = dict(row)

        cur.execute(f'UPDATE {table} SET is_billed=0, billed_quantity=0, bill_id=NULL '
                    f'WHERE id=%s', [cargo_source_id])

        # Which documents still reference it — recorded so the audit trail
        # explains why a bill and its cargo no longer agree.
        cur.execute('''SELECT COALESCE(STRING_AGG(DISTINCT bh.bill_number, ', '), '') AS bills,
                              COALESCE(STRING_AGG(DISTINCT ih.invoice_number, ', '), '') AS invoices
                       FROM bill_lines bl
                       LEFT JOIN bill_header bh ON bh.id = bl.bill_id
                       LEFT JOIN invoice_bill_mapping ibm ON ibm.bill_id = bh.id
                       LEFT JOIN invoice_header ih ON ih.id = ibm.invoice_id
                       WHERE bl.cargo_source_type=%s AND bl.cargo_source_id=%s''',
                    [cargo_source_type, cargo_source_id])
        held = dict(cur.fetchone() or {})

        comment = (f"Released {cargo_source_type} #{cargo_source_id} "
                   f"\"{before.get('cargo_name') or 'cargo'}\" "
                   f"({before.get('customer_name') or '?'}, "
                   f"{float(before.get('billed_quantity') or 0):.3f} billed) "
                   f"— bills: {held.get('bills') or '(none)'}; "
                   f"invoices: {held.get('invoices') or '(none)'} left unchanged"
                   + (f". Reason: {reason.strip()}" if reason and reason.strip() else ''))
        cur.execute("""INSERT INTO approval_log (module_code, record_id, action, comment, actioned_by)
                       VALUES ('FIN01', %s, 'Cargo released by Admin', %s, %s)""",
                    [cargo_source_id, comment, username])
        conn.commit()
        return {'ok': True,
                'cargo': before.get('cargo_name') or f'{cargo_source_type} #{cargo_source_id}',
                'customer': before.get('customer_name') or '',
                'bills': held.get('bills') or '',
                'invoices': held.get('invoices') or ''}
    except Exception as e:
        conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        conn.close()


def get_swappable_bills(search='', mismatched_only=False):
    """Bills carrying cargo lines, flagged when any line's declaration belongs to
    another party. Those flagged rows are exactly the bills this tab exists for."""
    term = f"%{(search or '').strip()}%"
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('''
            SELECT DISTINCT b.id, b.bill_number, b.bill_date, b.customer_name,
                   b.source_display, b.total_amount, b.bill_status,
                   COALESCE(STRING_AGG(DISTINCT ih.invoice_number, ', '), '') AS invoices
            FROM bill_header b
            JOIN bill_lines bl ON bl.bill_id = b.id AND bl.cargo_source_type IS NOT NULL
            LEFT JOIN invoice_bill_mapping ibm ON ibm.bill_id = b.id
            LEFT JOIN invoice_header ih ON ih.id = ibm.invoice_id
            WHERE b.bill_status <> 'Cancelled'
              AND (COALESCE(b.bill_number, '')    ILIKE %s
                OR COALESCE(b.customer_name, '')  ILIKE %s
                OR COALESCE(b.source_display, '') ILIKE %s)
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT 200
        ''', [term, term, term])
        bills = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for bill in bills:
        bad = [l for l in get_bill_cargo_lines(bill['id']) if l['party_mismatch']]
        bill['mismatch_count'] = len(bad)
    if mismatched_only:
        bills = [b for b in bills if b['mismatch_count']]
    return bills


def get_bill_cargo_lines(bill_id):
    """Cargo lines on a bill, each flagged if its declaration now belongs to
    someone other than the bill's customer. Drives the admin Swap Cargo tab."""
    conn = get_db()
    cur = get_cursor(conn)
    rows = []
    try:
        cur.execute('SELECT customer_name FROM bill_header WHERE id=%s', [bill_id])
        header = cur.fetchone()
        bill_customer = header['customer_name'] if header else ''

        for cargo_source_type, (table, qty_col) in _CARGO_TABLES.items():
            cur.execute(f'''
                SELECT bl.id AS bill_line_id, bl.cargo_source_type, bl.cargo_source_id,
                       bl.service_name, bl.service_description, bl.quantity, bl.uom,
                       bl.rate, bl.line_amount, bl.line_total,
                       d.customer_name AS cargo_customer, d.cargo_name
                FROM bill_lines bl
                JOIN {table} d ON d.id = bl.cargo_source_id
                WHERE bl.bill_id = %s AND bl.cargo_source_type = %s
                ORDER BY bl.id
            ''', [bill_id, cargo_source_type])
            for row in cur.fetchall():
                r = dict(row)
                r['bill_customer'] = bill_customer
                r['party_mismatch'] = not same_party(bill_customer, r['cargo_customer'])
                rows.append(r)
    finally:
        conn.close()
    return sorted(rows, key=lambda r: r['bill_line_id'])


def get_swap_candidates(bill_id):
    """Unbilled cargo belonging to this bill's own customer — the only cargo a
    swap may pull in. Filtering by the bill's customer here is what stops the
    tab from repeating the very mistake it exists to repair."""
    conn = get_db()
    cur = get_cursor(conn)
    rows = []
    try:
        cur.execute('SELECT customer_name FROM bill_header WHERE id=%s', [bill_id])
        header = cur.fetchone()
        if not header:
            return []
        customer = header['customer_name']

        for (cstype, table, owner_col, parent, doc_col, vessel_col, qty_col) in _RELEASE_SOURCES:
            cur.execute(f'''
                SELECT %s AS cargo_source_type, d.id AS cargo_source_id,
                       p.{doc_col} AS doc_num, p.{vessel_col} AS vessel_name,
                       d.cargo_name, d.customer_name,
                       COALESCE(d.{qty_col}, 0) AS total_quantity,
                       COALESCE(d.billed_quantity, 0) AS billed_quantity,
                       ROUND(COALESCE(d.{qty_col}, 0) - COALESCE(d.billed_quantity, 0), 3)
                           AS billable_quantity
                FROM {table} d
                JOIN {parent} p ON p.id = d.{owner_col}
                WHERE d.customer_name = %s
                  AND COALESCE(d.{qty_col}, 0) - COALESCE(d.billed_quantity, 0) > 0
                ORDER BY d.id DESC
            ''', [cstype, customer])
            rows.extend(dict(r) for r in cur.fetchall())
    finally:
        conn.close()
    return rows


def _matching_invoice_line(cur, bill_id, line):
    """The invoice line copied from this bill line, or None.

    invoice_lines carries no bill_line_id, so it is matched on the fields
    create_invoice_from_bills copies verbatim. Returns (invoice_id, row) only
    when the match is unambiguous — the caller refuses the swap otherwise
    rather than guess which line to rewrite."""
    cur.execute('''SELECT * FROM invoice_lines
                   WHERE bill_id = %s
                     AND COALESCE(service_name, '') = COALESCE(%s, '')
                     AND COALESCE(service_description, '') = COALESCE(%s, '')
                     AND quantity = %s AND rate = %s''',
                [bill_id, line.get('service_name'), line.get('service_description'),
                 line['quantity'], line['rate']])
    matches = [dict(r) for r in cur.fetchall()]
    if len(matches) != 1:
        return None, len(matches)
    return matches[0], 1


def swap_bill_cargo(bill_id, bill_line_id, replacements, username, reason=''):
    """Replace one cargo line on a bill with cargo belonging to that same bill's
    customer, keeping every amount identical.

    Built for the case where a declaration was retro-edited and a bill ended up
    charging one customer for another's cargo, after the invoice had already
    gone out. Because the replacements inherit the removed line's rate and must
    total the same quantity, the bill total, invoice total and SAP posting are
    provably unchanged — only the cargo backing the charge moves. The displaced
    cargo is released and becomes billable to its rightful customer.

    Refuses anything it cannot do exactly: a quantity that does not balance, a
    replacement belonging to another party, one that is already billed, or an
    invoice line it cannot identify unambiguously.

    replacements: [{'cargo_source_type', 'cargo_source_id', 'quantity'}]
    Returns {'ok': bool, 'error', ...}."""
    if not replacements:
        return {'ok': False, 'error': 'Select at least one replacement cargo line'}

    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('SELECT * FROM bill_header WHERE id=%s FOR UPDATE', [bill_id])
        row = cur.fetchone()
        if not row:
            return {'ok': False, 'error': 'Bill not found'}
        bill = dict(row)
        if bill.get('bill_status') == 'Cancelled':
            return {'ok': False, 'error': 'Bill is cancelled — nothing to swap'}

        cur.execute('SELECT * FROM bill_lines WHERE id=%s AND bill_id=%s',
                    [bill_line_id, bill_id])
        row = cur.fetchone()
        if not row:
            return {'ok': False, 'error': 'Bill line not found on this bill'}
        old = dict(row)
        if not old.get('cargo_source_type') or not old.get('cargo_source_id'):
            return {'ok': False, 'error': 'That line is not a cargo line'}

        quantities = [float(r.get('quantity') or 0) for r in replacements]
        if any(q <= 0 for q in quantities):
            return {'ok': False, 'error': 'Every replacement quantity must be greater than zero'}
        if not quantities_balance(old['quantity'], quantities):
            return {'ok': False,
                    'error': (f'Replacement quantity ({sum(quantities):.3f}) must equal the '
                              f'quantity being removed ({float(old["quantity"]):.3f}). '
                              f'A swap may not change the amount — use a credit note instead.')}

        # Every replacement must belong to this bill's customer and have room.
        for rep, qty in zip(replacements, quantities):
            entry = _CARGO_TABLES.get(rep.get('cargo_source_type'))
            if not entry:
                return {'ok': False, 'error': f"Unknown cargo type: {rep.get('cargo_source_type')}"}
            table, qty_col = entry
            cur.execute(f'SELECT customer_name, cargo_name, COALESCE(billed_quantity,0) AS billed, '
                        f'COALESCE({qty_col},0) AS total FROM {table} WHERE id=%s FOR UPDATE',
                        [rep.get('cargo_source_id')])
            decl = cur.fetchone()
            if not decl:
                return {'ok': False, 'error': f"Replacement cargo #{rep.get('cargo_source_id')} not found"}
            if not same_party(bill['customer_name'], decl['customer_name']):
                return {'ok': False,
                        'error': (f"\"{decl['cargo_name'] or 'cargo'}\" belongs to "
                                  f"{decl['customer_name']}, not {bill['customer_name']} — "
                                  f"a swap may only pull in this customer's own cargo.")}
            if would_overbill(decl['billed'], qty, decl['total']):
                return {'ok': False,
                        'error': (f"\"{decl['cargo_name'] or 'cargo'}\" only has "
                                  f"{float(decl['total']) - float(decl['billed']):.3f} left to bill.")}

        # Locate the invoice copy before changing anything, so an ambiguous
        # match aborts the swap rather than leaving bill and invoice disagreeing.
        cur.execute('SELECT invoice_id FROM invoice_bill_mapping WHERE bill_id=%s', [bill_id])
        invoice_ids = [r['invoice_id'] for r in cur.fetchall()]
        invoice_line = None
        if invoice_ids:
            invoice_line, count = _matching_invoice_line(cur, bill_id, old)
            if invoice_line is None:
                return {'ok': False,
                        'error': (f'Could not identify this line on the invoice ({count} candidates). '
                                  f'Swap aborted so the bill and invoice cannot drift apart — '
                                  f'correct this one with a credit note.')}
            invoice_before = money_sums(cur, 'invoice_lines', 'invoice_id',
                                        invoice_line['invoice_id'])

        bill_before = money_sums(cur, 'bill_lines', 'bill_id', bill_id)
        shares = split_line_amounts(old, quantities)

        # --- rewrite the bill line ---
        _unmark_cargo_source_billed(cur, old['cargo_source_type'],
                                    old['cargo_source_id'], float(old['quantity'] or 0))
        released_table = _CARGO_TABLES[old['cargo_source_type']][0]
        cur.execute(f'UPDATE {released_table} SET bill_id=NULL WHERE id=%s AND bill_id=%s',
                    [old['cargo_source_id'], bill_id])
        cur.execute('DELETE FROM bill_lines WHERE id=%s', [bill_line_id])

        new_line_ids = []
        for rep, qty, share in zip(replacements, quantities, shares):
            cur.execute('''INSERT INTO bill_lines
                (bill_id, cargo_source_type, cargo_source_id, service_record_id,
                 service_type_id, service_name, service_description, quantity, uom, rate,
                 line_amount, gst_rate_id, cgst_rate, sgst_rate, igst_rate,
                 cgst_amount, sgst_amount, igst_amount, line_total, gl_code, sac_code,
                 remarks, service_code, tds_applicable, tds_percent, tds_amount,
                 tcs_applicable, tcs_percent, tcs_amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id''',
                [bill_id, rep['cargo_source_type'], rep['cargo_source_id'],
                 old.get('service_record_id'), old.get('service_type_id'),
                 old.get('service_name'), rep.get('description') or old.get('service_description'),
                 qty, old.get('uom'), old.get('rate'),
                 share['line_amount'], old.get('gst_rate_id'), old.get('cgst_rate'),
                 old.get('sgst_rate'), old.get('igst_rate'),
                 share['cgst_amount'], share['sgst_amount'], share['igst_amount'],
                 share['line_total'], old.get('gl_code'), old.get('sac_code'),
                 old.get('remarks'), old.get('service_code'),
                 old.get('tds_applicable', 0), old.get('tds_percent', 0), share['tds_amount'],
                 old.get('tcs_applicable', 0), old.get('tcs_percent', 0), share['tcs_amount']])
            new_line_ids.append(cur.fetchone()['id'])
            _mark_cargo_source_billed(cur, rep['cargo_source_type'],
                                      rep['cargo_source_id'], qty, bill_id)

        # --- mirror onto the invoice so the print and cargo appendix agree ---
        if invoice_line:
            invoice_id = invoice_line['invoice_id']
            cur.execute('DELETE FROM invoice_lines WHERE id=%s', [invoice_line['id']])
            for rep, qty, share in zip(replacements, quantities, shares):
                cur.execute('''INSERT INTO invoice_lines
                    (invoice_id, bill_id, bill_number, line_number, service_name,
                     service_description, quantity, uom, rate, line_amount,
                     cgst_rate, sgst_rate, igst_rate, cgst_amount, sgst_amount, igst_amount,
                     line_total, gl_code, sac_code, profit_center, cost_center,
                     service_code, tds_applicable, tds_percent, tds_amount,
                     tcs_applicable, tcs_percent, tcs_amount)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    [invoice_id, bill_id, invoice_line.get('bill_number'),
                     invoice_line.get('line_number'), invoice_line.get('service_name'),
                     rep.get('description') or invoice_line.get('service_description'),
                     qty, invoice_line.get('uom'), invoice_line.get('rate'),
                     share['line_amount'], invoice_line.get('cgst_rate'),
                     invoice_line.get('sgst_rate'), invoice_line.get('igst_rate'),
                     share['cgst_amount'], share['sgst_amount'], share['igst_amount'],
                     share['line_total'], invoice_line.get('gl_code'),
                     invoice_line.get('sac_code'), invoice_line.get('profit_center'),
                     invoice_line.get('cost_center'), invoice_line.get('service_code'),
                     invoice_line.get('tds_applicable', 0), invoice_line.get('tds_percent', 0),
                     share['tds_amount'], invoice_line.get('tcs_applicable', 0),
                     invoice_line.get('tcs_percent', 0), share['tcs_amount']])

            # No GST reconciliation and no header rewrite. The shares preserve
            # each rate group's totals exactly, so the aggregate GST already
            # holds; re-deriving the header would also silently "correct" any
            # pre-existing drift, changing an invoice that has already gone out.
            drift = describe_sum_drift(
                invoice_before, money_sums(cur, 'invoice_lines', 'invoice_id', invoice_id))
            if drift:
                conn.rollback()
                return {'ok': False,
                        'error': (f'Swap would move the invoice amounts ({drift}). '
                                  f'Refused — raise a credit note instead.')}

        # Bill headers are left alone for the same reason; the lines must simply
        # add up to what they did before.
        drift = describe_sum_drift(bill_before, money_sums(cur, 'bill_lines', 'bill_id', bill_id))
        if drift:
            conn.rollback()
            return {'ok': False, 'error': f'Swap would move the bill amounts ({drift}). Refused.'}

        comment = (f"Swapped {old['cargo_source_type']} #{old['cargo_source_id']} "
                   f"\"{old.get('service_description') or 'cargo'}\" "
                   f"({float(old['quantity']):.3f} {old.get('uom') or ''}) out of {bill['bill_number']} "
                   f"for {len(replacements)} line(s) at the same rate; amounts unchanged "
                   f"(taxable {bill_before['taxable']:.2f}). "
                   f"Displaced cargo released and billable again."
                   + (f" Reason: {reason.strip()}" if reason and reason.strip() else ''))
        cur.execute("""INSERT INTO approval_log (module_code, record_id, action, comment, actioned_by)
                       VALUES ('FIN01', %s, 'Bill cargo swapped by Admin', %s, %s)""",
                    [bill_id, comment, username])

        conn.commit()
        return {'ok': True, 'bill_number': bill['bill_number'],
                'replaced_lines': len(new_line_ids),
                'total': round(bill_before['taxable'] + bill_before['cgst']
                               + bill_before['sgst'] + bill_before['igst'], 2),
                'invoice_updated': bool(invoice_line)}
    except Exception as e:
        conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        conn.close()


def money_sums(cur, table, key_column, key):
    """Total taxable + tax across a document's lines.

    The swap's invariant is measured here rather than on a header total, because
    a header can already disagree with its own lines (invoice DPPL/26-27/65 was
    stored ₹50 below the sum of its parts). Comparing line sums isolates what the
    swap actually changed from drift that was there beforehand."""
    cur.execute(f'''SELECT COALESCE(SUM(line_amount), 0)  AS taxable,
                           COALESCE(SUM(cgst_amount), 0)  AS cgst,
                           COALESCE(SUM(sgst_amount), 0)  AS sgst,
                           COALESCE(SUM(igst_amount), 0)  AS igst
                    FROM {table} WHERE {key_column} = %s''', [key])
    row = cur.fetchone()
    return {k: round(float(row[k]), 2) for k in ('taxable', 'cgst', 'sgst', 'igst')}


def describe_sum_drift(before, after):
    """'' when two money_sums snapshots match to the paisa, else what moved."""
    moved = [f'{k} {before[k]:.2f} → {after[k]:.2f}'
             for k in before if abs(before[k] - after[k]) > 0.005]
    return '; '.join(moved)


def get_removable_bills():
    """Bills eligible for admin removal: not invoiced, not linked to an invoice."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT b.id, b.bill_number, b.bill_date, b.customer_name, b.source_display,
               b.total_amount, b.bill_status, b.created_by
        FROM bill_header b
        WHERE b.bill_status NOT IN ('Invoiced', 'Cancelled')
          AND NOT EXISTS (SELECT 1 FROM invoice_bill_mapping ibm WHERE ibm.bill_id = b.id)
        ORDER BY b.id DESC
    ''')
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def delete_bill(bill_id, username):
    """Remove a bill entirely (admin duplicate cleanup) and free its number.

    Releases the cargo/service billed tracking the bill held (share-aware, see
    _release_bill_for_delete — a source another active bill still owns is left
    billed), deletes its lines + header so the BILL number is freed and the
    series continues with no gap, and writes an approval_log row. Refuses
    invoiced or invoice-linked bills — uninvoice those first.

    Returns {'ok': bool, 'bill_number', 'error'}."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('SELECT * FROM bill_header WHERE id=%s FOR UPDATE', [bill_id])
        row = cur.fetchone()
        bill = dict(row) if row else None
        if not bill:
            conn.close()
            return {'ok': False, 'error': 'Bill not found'}
        if bill.get('bill_status') == 'Invoiced':
            conn.close()
            return {'ok': False, 'error': 'Bill is invoiced — uninvoice it first'}
        cur.execute('SELECT 1 FROM invoice_bill_mapping WHERE bill_id=%s LIMIT 1', [bill_id])
        if cur.fetchone():
            conn.close()
            return {'ok': False, 'error': 'Bill is linked to an invoice — uninvoice it first'}

        _release_bill_for_delete(cur, bill_id)
        cur.execute('DELETE FROM bill_lines WHERE bill_id=%s', [bill_id])
        cur.execute('DELETE FROM bill_header WHERE id=%s', [bill_id])

        comment = (f"Deleted bill {bill['bill_number']} (was {bill.get('bill_status')}); "
                   f"billed tracking released (share-aware)")
        cur.execute("""INSERT INTO approval_log (module_code, record_id, action, comment, actioned_by)
                       VALUES ('FIN01', %s, 'Bill removed by Admin', %s, %s)""",
                    [bill_id, comment, username])
        conn.commit()
        return {'ok': True, 'bill_number': bill['bill_number']}
    except Exception as e:
        conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        conn.close()


def _can_revert_to_draft(bill):
    """(ok, reason) for pulling an approved bill back to Draft instead of deleting it.

    Draft and Approved bills both hold their cargo/service billed tracking, so
    the revert is status-only — nothing to release or re-apply."""
    if not bill:
        return False, 'Bill not found'
    if bill.get('bill_status') != 'Approved':
        return False, f"Only approved bills can be sent back to draft (this one is {bill.get('bill_status')})"
    return True, ''


def revert_bill_to_draft(bill_id, username):
    """Send an approved, uninvoiced bill back to Draft so it can be edited.

    Returns {'ok': bool, 'bill_number', 'error'}."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('SELECT * FROM bill_header WHERE id=%s FOR UPDATE', [bill_id])
        row = cur.fetchone()
        bill = dict(row) if row else None
        ok, reason = _can_revert_to_draft(bill)
        if not ok:
            conn.close()
            return {'ok': False, 'error': reason}
        cur.execute('SELECT 1 FROM invoice_bill_mapping WHERE bill_id=%s LIMIT 1', [bill_id])
        if cur.fetchone():
            conn.close()
            return {'ok': False, 'error': 'Bill is linked to an invoice — uninvoice it first'}

        cur.execute("""UPDATE bill_header
                       SET bill_status='Draft', approved_by=NULL, approved_date=NULL
                       WHERE id=%s""", [bill_id])
        # Repair any header/line drift while we are here (older bills could be
        # left with 0.00 GST by a half-written save).
        recalc_bill_totals(cur, bill_id)
        cur.execute("""INSERT INTO approval_log (module_code, record_id, action, comment, actioned_by)
                       VALUES ('FIN01', %s, 'Bill reverted to Draft by Admin', %s, %s)""",
                    [bill_id, f"Bill {bill['bill_number']} sent back from Approved to Draft", username])
        conn.commit()
        return {'ok': True, 'bill_number': bill['bill_number']}
    except Exception as e:
        conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        conn.close()


def bill_totals(lines):
    """Header totals from the bill's own stored lines.

    The header is never allowed to carry numbers its lines do not back: GST is
    derived per line by save_bill_line (from the service master), so summing the
    request payload — which carries no GST at all — is what once produced a bill
    with correct line GST and a 0.00 header."""
    subtotal = sum(float(l.get('line_amount') or 0) for l in lines)
    cgst = sum(float(l.get('cgst_amount') or 0) for l in lines)
    sgst = sum(float(l.get('sgst_amount') or 0) for l in lines)
    igst = sum(float(l.get('igst_amount') or 0) for l in lines)
    tds = sum(float(l.get('tds_amount') or 0) for l in lines)
    tcs = sum(float(l.get('tcs_amount') or 0) for l in lines)
    # TCS is collected from the customer, so it is part of what they owe and
    # belongs in total_amount. TDS is their own deduction at payment time —
    # informational, never subtracted here. The SAP payload does NOT read
    # total_amount; it rebuilds taxable + GST from the components (see
    # sap_builder._total_invoice_amount) so this convention cannot leak into it.
    return {'subtotal': round(subtotal, 2), 'cgst_amount': round(cgst, 2),
            'sgst_amount': round(sgst, 2), 'igst_amount': round(igst, 2),
            'tds_amount': round(tds, 2), 'tcs_amount': round(tcs, 2),
            'total_amount': round(subtotal + cgst + sgst + igst + tcs, 2)}


def recalc_bill_totals(cur, bill_id):
    """Rewrite a bill header's totals from its stored lines. Caller commits."""
    cur.execute("""SELECT line_amount, cgst_amount, sgst_amount, igst_amount,
                          tds_amount, tcs_amount
                   FROM bill_lines WHERE bill_id=%s""", [bill_id])
    t = bill_totals([dict(r) for r in cur.fetchall()])
    cur.execute("""UPDATE bill_header
                   SET subtotal=%s, cgst_amount=%s, sgst_amount=%s,
                       igst_amount=%s, tds_amount=%s, tcs_amount=%s, total_amount=%s
                   WHERE id=%s""",
                [t['subtotal'], t['cgst_amount'], t['sgst_amount'], t['igst_amount'],
                 t['tds_amount'], t['tcs_amount'], t['total_amount'], bill_id])
    return t


def get_bill_by_id(bill_id):
    """Get bill header by ID"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT
            b.*,
            ca.agreement_code,
            ca.agreement_name,
            NULLIF(
                TRIM(
                    COALESCE(ca.agreement_code, '') ||
                    CASE
                        WHEN COALESCE(ca.agreement_name, '') <> '' THEN ' - ' || ca.agreement_name
                        ELSE ''
                    END
                ),
                ''
            ) AS agreement_display
        FROM bill_header b
        LEFT JOIN customer_agreements ca ON b.agreement_id = ca.id
        WHERE b.id = %s
    ''', (bill_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def quantity_totals(lines):
    """Billed quantity per UOM, as [(uom, total)] sorted by uom.

    Kept per UOM because a bill mixes cargo tonnage with service records
    counted in their own units — one grand total would be a nonsense number."""
    totals = {}
    for ln in lines:
        uom = (ln.get('uom') or '').strip()
        totals[uom] = totals.get(uom, 0.0) + float(ln.get('quantity') or 0)
    return [(uom, round(q, 3)) for uom, q in sorted(totals.items())]


def get_bill_lines(bill_id):
    """Get all lines for a bill"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM bill_lines WHERE bill_id = %s ORDER BY id', (bill_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_bill_line(data, cur=None):
    """Save bill line (supports both EU lines and service records).

    Pass the caller's cursor to join its transaction (nothing is committed
    then — the caller owns commit/rollback)."""
    conn = None if cur is not None else get_db()
    if conn is not None:
        cur = get_cursor(conn)
    existing_line = None

    # Look up TDS/TCS config from service master (FSTM01)
    tds_applicable = int(data.get('tds_applicable') or 0)
    tds_percent = float(data.get('tds_percent') or 0)
    tds_amount = float(data.get('tds_amount') or 0)
    tcs_applicable = int(data.get('tcs_applicable') or 0)
    tcs_percent = float(data.get('tcs_percent') or 0)
    tcs_amount = float(data.get('tcs_amount') or 0)
    service_code = data.get('service_code') or ''

    svc_id = data.get('service_type_id')
    if svc_id:
        cur.execute(
            'SELECT service_code, is_tds, tds_percent, is_tcs, tcs_percent, gst_rate_id, sac_code, sap_gl_account, gl_code FROM finance_service_types WHERE id = %s',
            [svc_id]
        )
        svc = cur.fetchone()
        if svc:
            service_code = service_code or (svc.get('service_code') or '')
            if not data.get('sac_code'):
                data['sac_code'] = svc.get('sac_code') or ''
            if not data.get('gl_code'):
                data['gl_code'] = svc.get('sap_gl_account') or svc.get('gl_code') or ''
            # TDS — calculated on basic amount only
            if not data.get('tds_applicable') and svc.get('is_tds'):
                tds_applicable = 1
                tds_percent = float(svc.get('tds_percent') or 0)
                line_amount = float(data.get('line_amount') or 0)
                tds_amount = round(line_amount * tds_percent / 100, 2)
            # TCS — calculated on basic + GST (set after GST computation below)
            if not data.get('tcs_applicable') and svc.get('is_tcs'):
                tcs_applicable = 1
                tcs_percent = float(svc.get('tcs_percent') or 0)
            # GST — auto-compute if not already provided
            gst_rate_id = svc.get('gst_rate_id')
            if gst_rate_id and not data.get('cgst_amount') and not data.get('igst_amount'):
                cur.execute('SELECT cgst_rate, sgst_rate, igst_rate FROM gst_rates WHERE id = %s', [gst_rate_id])
                gst = cur.fetchone()
                if gst:
                    line_amount = float(data.get('line_amount') or 0)
                    data['gst_rate_id'] = gst_rate_id
                    # Determine CGST+SGST vs IGST
                    customer_gstin = data.get('customer_gstin') or ''
                    customer_state = data.get('customer_state_code') or ''
                    # Get port state code from FIN01 module config (seller_gstin / port_gst_state_code)
                    from database import get_module_config
                    fin_cfg = get_module_config('FIN01')
                    port_state_code = str(fin_cfg.get('port_gst_state_code') or '').strip()
                    seller_gstin = str(fin_cfg.get('seller_gstin') or '').strip()
                    # Derive port state from explicit config first, then GSTIN prefix
                    if not port_state_code and seller_gstin:
                        port_state_code = seller_gstin[:2]
                    # Compare state codes
                    if customer_state and port_state_code:
                        same_state = customer_state.strip() == port_state_code
                    elif customer_gstin and port_state_code:
                        same_state = customer_gstin[:2] == port_state_code
                    else:
                        # Cannot determine — default to intra-state (safer: no IGST surprise)
                        same_state = True
                    if same_state:
                        # Intra-state: CGST + SGST
                        data['cgst_rate'] = float(gst['cgst_rate'] or 0)
                        data['sgst_rate'] = float(gst['sgst_rate'] or 0)
                        data['igst_rate'] = 0
                        data['cgst_amount'] = round(line_amount * data['cgst_rate'] / 100, 2)
                        data['sgst_amount'] = round(line_amount * data['sgst_rate'] / 100, 2)
                        data['igst_amount'] = 0
                    else:
                        # Inter-state: IGST
                        data['cgst_rate'] = 0
                        data['sgst_rate'] = 0
                        data['igst_rate'] = float(gst['igst_rate'] or 0)
                        data['cgst_amount'] = 0
                        data['sgst_amount'] = 0
                        data['igst_amount'] = round(line_amount * data['igst_rate'] / 100, 2)

    # Compute line_total = line_amount + GST
    la = float(data.get('line_amount') or 0)
    ca = float(data.get('cgst_amount') or 0)
    sa = float(data.get('sgst_amount') or 0)
    ia = float(data.get('igst_amount') or 0)
    data['line_total'] = round(la + ca + sa + ia, 2)

    # TCS — calculated on basic + GST
    if tcs_applicable and tcs_percent > 0:
        tcs_amount = round((la + ca + sa + ia) * tcs_percent / 100, 2)

    if data.get('id'):
        cur.execute(
            'SELECT cargo_source_type, cargo_source_id, quantity FROM bill_lines WHERE id=%s',
            [data['id']]
        )
        existing_line = cur.fetchone()
        if existing_line:
            _unmark_cargo_source_billed(
                cur,
                existing_line.get('cargo_source_type'),
                existing_line.get('cargo_source_id'),
                float(existing_line.get('quantity') or 0)
            )
        cur.execute('''UPDATE bill_lines
            SET cargo_source_type=%s, cargo_source_id=%s, service_record_id=%s, service_type_id=%s, service_name=%s,
                service_description=%s, quantity=%s, uom=%s, rate=%s, line_amount=%s,
                gst_rate_id=%s, cgst_rate=%s, sgst_rate=%s, igst_rate=%s,
                cgst_amount=%s, sgst_amount=%s, igst_amount=%s,
                line_total=%s, gl_code=%s, sac_code=%s, remarks=%s,
                service_code=%s, tds_applicable=%s, tds_percent=%s, tds_amount=%s,
                tcs_applicable=%s, tcs_percent=%s, tcs_amount=%s
            WHERE id=%s''',
            [data.get('cargo_source_type'), data.get('cargo_source_id'), data.get('service_record_id'),
             data.get('service_type_id'), data.get('service_name'),
             data.get('service_description'), data.get('quantity'), data.get('uom'),
             data.get('rate'), data.get('line_amount'), data.get('gst_rate_id'),
             data.get('cgst_rate'), data.get('sgst_rate'), data.get('igst_rate'),
             data.get('cgst_amount'), data.get('sgst_amount'), data.get('igst_amount'),
             data.get('line_total'), data.get('gl_code'), data.get('sac_code'),
             data.get('remarks'), service_code, tds_applicable, tds_percent, tds_amount,
             tcs_applicable, tcs_percent, tcs_amount,
             data['id']])
        row_id = data['id']
    else:
        cur.execute('''INSERT INTO bill_lines
            (bill_id, cargo_source_type, cargo_source_id, service_record_id, service_type_id, service_name,
             service_description, quantity, uom, rate, line_amount, gst_rate_id,
             cgst_rate, sgst_rate, igst_rate, cgst_amount, sgst_amount, igst_amount,
             line_total, gl_code, sac_code, remarks,
             service_code, tds_applicable, tds_percent, tds_amount,
             tcs_applicable, tcs_percent, tcs_amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id''',
            [data['bill_id'], data.get('cargo_source_type'), data.get('cargo_source_id'), data.get('service_record_id'),
             data.get('service_type_id'), data.get('service_name'),
             data.get('service_description'),
             data.get('quantity'), data.get('uom'), data.get('rate'), data.get('line_amount'),
             data.get('gst_rate_id'), data.get('cgst_rate'), data.get('sgst_rate'),
             data.get('igst_rate'), data.get('cgst_amount'), data.get('sgst_amount'),
             data.get('igst_amount'), data.get('line_total'), data.get('gl_code'),
             data.get('sac_code'), data.get('remarks'),
             service_code, tds_applicable, tds_percent, tds_amount,
             tcs_applicable, tcs_percent, tcs_amount])
        row_id = cur.fetchone()['id']

    # Mark cargo declaration source as billed
    _mark_cargo_source_billed(
        cur,
        data.get('cargo_source_type'),
        data.get('cargo_source_id'),
        float(data.get('quantity') or 0),
        data.get('bill_id')
    )

    # Mark the service record as billed if service_record_id is provided
    if data.get('service_record_id'):
        _mark_service_record_billed(cur, data.get('service_record_id'),
                                    float(data.get('quantity') or 0), data.get('bill_id'))

    if conn is not None:
        conn.commit()
        conn.close()
    return row_id


def delete_bill_line(row_id):
    """Delete bill line and reverse billed tracking on cargo source."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(
        'SELECT cargo_source_type, cargo_source_id, quantity, service_record_id FROM bill_lines WHERE id=%s',
        (row_id,)
    )
    bl = cur.fetchone()
    if bl:
        _unmark_cargo_source_billed(
            cur,
            bl['cargo_source_type'],
            bl['cargo_source_id'],
            float(bl['quantity'] or 0)
        )
        if bl.get('service_record_id'):
            _unmark_service_record_billed(cur, bl['service_record_id'],
                                          float(bl['quantity'] or 0))
    cur.execute('DELETE FROM bill_lines WHERE id=%s', (row_id,))
    conn.commit()
    conn.close()


# ===== INVOICE FUNCTIONS =====

def get_next_invoice_number(series='INV'):
    """Generate next invoice number"""
    year = datetime.now().year
    prefix = f"{series}{year}-"

    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(
        "SELECT MAX(CAST(SUBSTR(invoice_number, LENGTH(%s) + 1) AS INTEGER)) FROM invoice_header WHERE invoice_number LIKE %s",
        [prefix, f"{prefix}%"]
    )
    result = cur.fetchone()['max']
    conn.close()
    next_num = (result or 0) + 1
    return f"{prefix}{next_num:04d}"


def get_financial_year(date_str):
    """Get financial year from date (FY runs Apr-Mar)"""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    if dt.month >= 4:
        return f"{dt.year}-{str(dt.year + 1)[2:]}"
    else:
        return f"{dt.year - 1}-{str(dt.year)[2:]}"


def get_invoice_data(page=1, size=20, status_filter=None):
    """Get paginated invoices"""
    conn = get_db()
    cur = get_cursor(conn)

    where_clause = ""
    params = []
    if status_filter:
        where_clause = "WHERE invoice_status = %s"
        params.append(status_filter)

    cur.execute(f'SELECT COUNT(*) FROM invoice_header {where_clause}', params)
    total = cur.fetchone()['count']
    cur.execute(f'''
        SELECT * FROM invoice_header {where_clause}
        ORDER BY id DESC
        LIMIT %s OFFSET %s
    ''', params + [size, (page-1)*size])
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def compute_aggregate_gst(lines):
    """Compute GST on the aggregated taxable **per rate group**, rounded once
    (the GST-compliant method), and redistribute each group's tax across its
    lines so the per-line amounts still sum exactly to the group total.

    This is what makes SAP auto-post: SAP re-derives tax as round(base x rate)
    on the aggregated line, so summing per-line-rounded tax (which drifts a
    paisa or two) never matches. Rounding once on the aggregate does.

    `lines`: dicts with keys id, line_amount, cgst_rate, sgst_rate, igst_rate.
    Returns (line_gst, totals):
      line_gst = {id: {cgst_amount, sgst_amount, igst_amount, line_total}}
      totals   = {subtotal, cgst_amount, sgst_amount, igst_amount}
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for ln in lines:
        key = (round(float(ln.get('cgst_rate') or 0), 4),
               round(float(ln.get('sgst_rate') or 0), 4),
               round(float(ln.get('igst_rate') or 0), 4))
        groups[key].append(ln)

    line_gst = {ln['id']: {'cgst_amount': 0.0, 'sgst_amount': 0.0, 'igst_amount': 0.0}
                for ln in lines}

    for (cr, sr, ir), glines in groups.items():
        taxable = sum(float(l.get('line_amount') or 0) for l in glines)
        for col, rate in (('cgst_amount', cr), ('sgst_amount', sr), ('igst_amount', ir)):
            if rate <= 0:
                continue
            target   = round(taxable * rate / 100, 2)
            per_line = [round(float(l.get('line_amount') or 0) * rate / 100, 2) for l in glines]
            residual = round(target - sum(per_line), 2)
            if residual:
                # Push the leftover paisa onto the largest line — least visible,
                # deterministic, and keeps the group sum exact.
                idx = max(range(len(glines)),
                          key=lambda i: float(glines[i].get('line_amount') or 0))
                per_line[idx] = round(per_line[idx] + residual, 2)
            for l, val in zip(glines, per_line):
                line_gst[l['id']][col] = val

    subtotal = cg = sg = ig = 0.0
    for ln in lines:
        amt = float(ln.get('line_amount') or 0)
        g = line_gst[ln['id']]
        g['line_total'] = round(amt + g['cgst_amount'] + g['sgst_amount'] + g['igst_amount'], 2)
        subtotal += amt
        cg += g['cgst_amount']; sg += g['sgst_amount']; ig += g['igst_amount']
    totals = {'subtotal': round(subtotal, 2), 'cgst_amount': round(cg, 2),
              'sgst_amount': round(sg, 2), 'igst_amount': round(ig, 2)}
    return line_gst, totals


def _amount_in_words(amount):
    """Indian-format rupee words, mirroring the FINV01 frontend so a
    server-recomputed total keeps a matching 'Amount in Words'."""
    ones = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight',
            'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen',
            'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen']
    tens = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy',
            'Eighty', 'Ninety']

    def to_words(n):
        if n == 0:
            return ''
        if n < 20:
            return ones[n] + ' '
        if n < 100:
            return tens[n // 10] + ' ' + (ones[n % 10] + ' ' if n % 10 else '')
        if n < 1000:
            return ones[n // 100] + ' Hundred ' + to_words(n % 100)
        if n < 100000:
            return to_words(n // 1000) + 'Thousand ' + to_words(n % 1000)
        if n < 10000000:
            return to_words(n // 100000) + 'Lakh ' + to_words(n % 100000)
        return to_words(n // 10000000) + 'Crore ' + to_words(n % 10000000)

    rounded = int(round(float(amount or 0) * 100))
    rupees, paise = divmod(rounded, 100)
    words = 'Rupees ' + to_words(rupees).strip()
    if paise > 0:
        words += ' and ' + to_words(paise).strip() + ' Paise'
    return words + ' Only'


def _reconcile_invoice_gst(cur, invoice_id):
    """Rewrite an invoice's GST to the aggregate-per-rate figures (see
    compute_aggregate_gst) across its lines, header totals and amount-in-words,
    so the print, SAC summary and SAP payload all agree with SAP's own
    round(base x rate). Runs inside the caller's transaction; does NOT commit."""
    cur.execute('''SELECT id, line_amount, cgst_rate, sgst_rate, igst_rate
                   FROM invoice_lines WHERE invoice_id=%s ORDER BY id''', [invoice_id])
    lines = [dict(r) for r in cur.fetchall()]
    if not lines:
        return
    line_gst, totals = compute_aggregate_gst(lines)
    for lid, g in line_gst.items():
        cur.execute('''UPDATE invoice_lines
            SET cgst_amount=%s, sgst_amount=%s, igst_amount=%s, line_total=%s
            WHERE id=%s''',
            [g['cgst_amount'], g['sgst_amount'], g['igst_amount'], g['line_total'], lid])

    cur.execute('SELECT round_off, tcs_amount FROM invoice_header WHERE id=%s', [invoice_id])
    r = cur.fetchone()
    round_off = float((r['round_off'] if r else 0) or 0)
    # TCS is collected from the customer — same convention as bill_totals().
    tcs = float((r['tcs_amount'] if r else 0) or 0)
    total = round(totals['subtotal'] + totals['cgst_amount'] + totals['sgst_amount']
                  + totals['igst_amount'] + tcs + round_off, 2)
    cur.execute('''UPDATE invoice_header
        SET subtotal=%s, cgst_amount=%s, sgst_amount=%s, igst_amount=%s,
            total_amount=%s, amount_in_words=%s
        WHERE id=%s''',
        [totals['subtotal'], totals['cgst_amount'], totals['sgst_amount'],
         totals['igst_amount'], total, _amount_in_words(total), invoice_id])


def create_invoice_from_bills(bill_ids, invoice_data):
    """Create invoice from approved bills"""
    conn = get_db()
    cur = get_cursor(conn)

    # Get customer details from first bill (all bills should be for same customer)
    cur.execute('SELECT * FROM bill_header WHERE id=%s', (bill_ids[0],))
    first_bill = dict(cur.fetchone())

    # Add customer details from bill to invoice_data
    invoice_data['customer_id'] = first_bill['customer_id']
    invoice_data['customer_type'] = first_bill['customer_type']
    invoice_data['customer_name'] = first_bill['customer_name']
    invoice_data['customer_gstin'] = first_bill['customer_gstin']
    invoice_data['customer_gst_state_code'] = first_bill['customer_gst_state_code']
    invoice_data['customer_gl_code'] = first_bill['customer_gl_code']

    # Generate invoice number and FY
    if invoice_data.get('_invoice_number_override'):
        invoice_number = invoice_data.pop('_invoice_number_override')
    else:
        invoice_number = get_next_invoice_number(invoice_data.get('invoice_series', 'INV'))
    financial_year = get_financial_year(invoice_data['invoice_date'])

    invoice_data['invoice_number'] = invoice_number
    invoice_data['financial_year'] = financial_year

    # Insert invoice header
    cols = [k for k in invoice_data if k not in ('id', '_invoice_number_override')]
    cur.execute(f'''INSERT INTO invoice_header
        ({', '.join(cols)})
        VALUES ({', '.join(['%s']*len(cols))})
        RETURNING id''',
        [invoice_data[c] for c in cols])
    invoice_id = cur.fetchone()['id']

    # Copy bill lines to invoice lines
    line_number = 1
    for bill_id in bill_ids:
        # Get bill details
        cur.execute('SELECT * FROM bill_header WHERE id=%s', (bill_id,))
        bill = dict(cur.fetchone())

        # Create mapping entry
        cur.execute('''INSERT INTO invoice_bill_mapping
            (invoice_id, bill_id, bill_number, bill_amount)
            VALUES (%s, %s, %s, %s)''',
            [invoice_id, bill_id, bill['bill_number'], bill['total_amount']])

        # Copy bill lines to invoice lines
        cur.execute('SELECT * FROM bill_lines WHERE bill_id=%s', (bill_id,))
        bill_lines = cur.fetchall()
        for bl in bill_lines:
            bl = dict(bl)
            cur.execute('''INSERT INTO invoice_lines
                (invoice_id, bill_id, bill_number, line_number, service_name, service_description,
                 quantity, uom, rate, line_amount, cgst_rate, sgst_rate, igst_rate,
                 cgst_amount, sgst_amount, igst_amount, line_total, gl_code, sac_code,
                 profit_center, cost_center,
                 service_code, tds_applicable, tds_percent, tds_amount,
                 tcs_applicable, tcs_percent, tcs_amount)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                [invoice_id, bill_id, bill['bill_number'], line_number, bl['service_name'],
                 bl['service_description'], bl['quantity'], bl['uom'], bl['rate'],
                 bl['line_amount'], bl['cgst_rate'], bl['sgst_rate'], bl['igst_rate'],
                 bl['cgst_amount'], bl['sgst_amount'], bl['igst_amount'], bl['line_total'],
                 bl['gl_code'], bl['sac_code'], invoice_data.get('profit_center'),
                 invoice_data.get('cost_center'),
                 bl.get('service_code'), bl.get('tds_applicable', 0),
                 bl.get('tds_percent', 0), bl.get('tds_amount', 0),
                 bl.get('tcs_applicable', 0), bl.get('tcs_percent', 0),
                 bl.get('tcs_amount', 0)])
            line_number += 1

        # Mark bill as invoiced
        cur.execute("UPDATE bill_header SET bill_status='Invoiced' WHERE id=%s", (bill_id,))

    # Auto-calculate invoice header tds_amount and tcs_amount from line totals
    cur.execute(
        'SELECT COALESCE(SUM(tds_amount), 0) AS total_tds, COALESCE(SUM(tcs_amount), 0) AS total_tcs FROM invoice_lines WHERE invoice_id = %s',
        [invoice_id]
    )
    row = cur.fetchone()
    total_tds = row['total_tds']
    total_tcs = row['total_tcs']
    if total_tds or total_tcs:
        cur.execute(
            'UPDATE invoice_header SET tds_amount = %s, tcs_amount = %s WHERE id = %s',
            [total_tds, total_tcs, invoice_id]
        )

    # Recompute GST on the aggregated taxable per rate so the invoice and the
    # SAP payload match SAP's own round(base x rate) — fixes the paisa mismatch
    # that blocked auto-posting.
    _reconcile_invoice_gst(cur, invoice_id)

    conn.commit()
    conn.close()
    return invoice_id, invoice_number


def _can_uninvoice(invoice):
    """Pure guard: may this invoice be removed via the admin Uninvoice tab?

    Only invoices that never got a real SAP document (staging push only, or
    not yet pushed) can be uninvoiced — a whitespace-only value counts as no
    document. Anything already cancelled is refused. Returns (bool, reason)."""
    if not invoice:
        return False, 'Invoice not found'
    if (invoice.get('sap_document_number') or '').strip():
        return False, 'Invoice has a SAP document number — cancel it via FINV01 instead'
    if invoice.get('invoice_status') == 'Cancelled':
        return False, 'Invoice is already cancelled'
    return True, ''


def get_uninvoiceable_invoices():
    """Invoices with no SAP document number, for the admin Uninvoice tab."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT ih.id, ih.invoice_number, ih.invoice_date, ih.customer_name,
               ih.total_amount, ih.invoice_status, ih.created_by,
               COALESCE(string_agg(ibm.bill_number, ', ' ORDER BY ibm.id), '') AS bills
        FROM invoice_header ih
        LEFT JOIN invoice_bill_mapping ibm ON ibm.invoice_id = ih.id
        WHERE COALESCE(ih.sap_document_number, '') = ''
          AND ih.invoice_status <> 'Cancelled'
        GROUP BY ih.id
        ORDER BY ih.id DESC
    ''')
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def uninvoice_invoice(invoice_id, username):
    """Remove a mistakenly-generated invoice that was never posted to SAP.

    Deletes the invoice (header + lines + mapping) so its doc_series_seq is
    freed and the number can be reused with no gap, resets the linked bills
    from 'Invoiced' back to 'Approved' so they can be re-invoiced, and drops any
    non-sent SAP push job so the worker won't post it later. Cargo declarations
    and service_records are left untouched — the bills stay valid and billed.
    Refuses if a real SAP document exists (see _can_uninvoice).

    Returns {'ok': bool, 'invoice_number', 'bills': [...], 'error'}."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('SELECT * FROM invoice_header WHERE id=%s FOR UPDATE', [invoice_id])
        row = cur.fetchone()
        invoice = dict(row) if row else None
        ok, reason = _can_uninvoice(invoice)
        if not ok:
            conn.close()
            return {'ok': False, 'error': reason}

        cur.execute('SELECT bill_id, bill_number FROM invoice_bill_mapping WHERE invoice_id=%s',
                    [invoice_id])
        bills = [dict(r) for r in cur.fetchall()]

        for b in bills:
            cur.execute("UPDATE bill_header SET bill_status='Approved' WHERE id=%s", [b['bill_id']])

        # Drop any queued/failed (or accepted-staging) SAP push for this invoice.
        cur.execute('DELETE FROM sap_outbound_queue WHERE invoice_id=%s', [invoice_id])

        cur.execute('DELETE FROM invoice_lines WHERE invoice_id=%s', [invoice_id])
        cur.execute('DELETE FROM invoice_bill_mapping WHERE invoice_id=%s', [invoice_id])
        cur.execute('DELETE FROM invoice_header WHERE id=%s', [invoice_id])

        bill_nums = ', '.join(b['bill_number'] for b in bills) or '(none)'
        comment = (f"Deleted invoice {invoice['invoice_number']} (no SAP doc); "
                   f"bills reset to Approved: {bill_nums}")
        cur.execute("""INSERT INTO approval_log (module_code, record_id, action, comment, actioned_by)
                       VALUES ('FINV01', %s, 'Uninvoiced by Admin', %s, %s)""",
                    [invoice_id, comment, username])
        conn.commit()
        return {'ok': True, 'invoice_number': invoice['invoice_number'],
                'bills': [b['bill_number'] for b in bills]}
    except Exception as e:
        conn.rollback()
        return {'ok': False, 'error': str(e)}
    finally:
        conn.close()


def get_invoice_lines(invoice_id):
    """Get all lines for an invoice"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM invoice_lines WHERE invoice_id = %s ORDER BY line_number', (invoice_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_invoice_bills(invoice_id):
    """Get all bills included in an invoice"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT m.*, b.bill_date, b.customer_name
        FROM invoice_bill_mapping m
        JOIN bill_header b ON m.bill_id = b.id
        WHERE m.invoice_id = %s
        ORDER BY b.bill_date
    ''', (invoice_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_invoice_by_id(invoice_id):
    """Get invoice header by ID"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM invoice_header WHERE id = %s', (invoice_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_invoice_sac_summary(invoice_id):
    """Get SAC-wise summary for invoice (grouped by SAC code)"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT
            il.sac_code,
            SUM(il.line_amount) as taxable_value,
            SUM(il.cgst_amount) as cgst,
            SUM(il.sgst_amount) as sgst,
            SUM(il.igst_amount) as igst
        FROM invoice_lines il
        WHERE il.invoice_id = %s
        GROUP BY il.sac_code
        ORDER BY il.sac_code
    ''', (invoice_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]
