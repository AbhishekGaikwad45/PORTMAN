"""Go-live cutover logic: seed numbers, mark items billed, lock. Pure helpers
have no DB dependency so they are unit-testable; DB functions open their own
connection like the rest of the codebase."""
from database import get_db, get_cursor, get_module_config, save_module_config
from decimal import Decimal, ROUND_HALF_UP
import json

# cargo_source_type -> (declaration table, total-quantity column)
CARGO_SOURCES = {
    'VCN_IMPORT': ('vcn_cargo_declaration', 'bl_quantity'),
    'VCN_EXPORT': ('vcn_export_cargo_declaration', 'bl_quantity'),
    'MBC':        ('mbc_customer_details', 'quantity'),
}


def cargo_source(source_type):
    """Map a cargo_source_type to its (table, qty_column), or None if unknown."""
    return CARGO_SOURCES.get(source_type)


def validate_start_seq(start_seq, current_max):
    """A cutover start number must be a positive integer strictly greater than
    the highest number already issued (else it would be silently ignored)."""
    if not isinstance(start_seq, int) or start_seq <= 0:
        return False, 'Start number must be a positive integer.'
    if start_seq <= (current_max or 0):
        return False, (f'Start number must be greater than the highest number '
                       f'already issued ({current_max or 0}).')
    return True, ''


def compute_partial_billed(total, already, bill_qty):
    """New (billed_quantity, is_billed) for a partial cutover mark.

    total    -- declared total quantity on the row
    already  -- quantity already billed
    bill_qty -- quantity to mark billed now; None or <= 0 means "mark the whole
                remaining balance" (preserves the original all-or-nothing
                behaviour). Capped so we never bill past the total.

    Mirrors FIN01's _mark_cargo_source_billed: is_billed flips to 1 only once the
    accumulated billed quantity reaches the total."""
    # Round inputs to 3 dp up front so the is_billed comparison below is at the
    # same precision as new_billed (and as FIN01's billable_quantity); otherwise a
    # bl_quantity with >3 decimals could never flip is_billed to 1 on a full mark.
    total = round(float(total or 0), 3)
    already = round(float(already or 0), 3)
    balance = max(total - already, 0)
    if bill_qty in (None, '') or float(bill_qty) <= 0:
        bill_qty = balance
    else:
        bill_qty = min(float(bill_qty), balance)
    new_billed = float(
        Decimal(str(already + bill_qty)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
    )
    is_billed = 1 if new_billed >= total else 0
    return new_billed, is_billed


# ===== Lock state + audit =====

def is_locked():
    cfg = get_module_config('ADMIN') or {}
    return str(cfg.get('cutover_locked', '0')) == '1'


def set_lock(locked, username):
    cfg = get_module_config('ADMIN') or {}
    cfg['cutover_locked'] = '1' if locked else '0'
    save_module_config('ADMIN', cfg)
    write_audit('lock' if locked else 'unlock', {}, username)


def write_audit(action, details, username):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(
        'INSERT INTO cutover_audit (action, details, performed_by) VALUES (%s, %s, %s)',
        [action, json.dumps(details), username])
    conn.commit()
    conn.close()


# ===== Seed read/write =====

def get_seeds():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM cutover_seed ORDER BY seed_type, doc_series, financial_year')
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _current_invoice_max(cur, doc_series, financial_year):
    cur.execute(
        'SELECT MAX(doc_series_seq) AS m FROM invoice_header WHERE doc_series=%s AND financial_year=%s',
        [doc_series, financial_year])
    return (cur.fetchone()['m'] or 0)


def _current_bill_max(cur):
    cur.execute(
        "SELECT MAX(CAST(SUBSTR(bill_number, 5) AS INTEGER)) AS m FROM bill_header WHERE bill_number LIKE 'BILL%%'")
    return (cur.fetchone()['m'] or 0)


def _current_fdcn_max(cur, doc_series, financial_year):
    cur.execute(
        'SELECT MAX(doc_series_seq) AS m FROM fdcn_header WHERE doc_series=%s AND financial_year=%s',
        [doc_series, financial_year])
    return (cur.fetchone()['m'] or 0)


def set_invoice_seed(doc_series, financial_year, start_seq, username):
    """Upsert an invoice seed after validating against the current max.
    Returns (ok, message)."""
    if is_locked():
        return False, 'Cutover is locked.'
    conn = get_db()
    cur = get_cursor(conn)
    current_max = _current_invoice_max(cur, doc_series, financial_year)
    ok, msg = validate_start_seq(start_seq, current_max)
    if not ok:
        conn.close()
        return False, msg
    cur.execute('''
        INSERT INTO cutover_seed (seed_type, doc_series, financial_year, start_seq, created_by, updated_by, updated_at)
        VALUES ('invoice', %s, %s, %s, %s, %s, now())
        ON CONFLICT (seed_type, doc_series, financial_year)
        DO UPDATE SET start_seq=EXCLUDED.start_seq, updated_by=EXCLUDED.updated_by, updated_at=now()
    ''', [doc_series, financial_year, start_seq, username, username])
    conn.commit()
    conn.close()
    write_audit('set_invoice_seed',
                {'doc_series': doc_series, 'financial_year': financial_year, 'start_seq': start_seq},
                username)
    return True, ''


def set_bill_seed(start_seq, username):
    if is_locked():
        return False, 'Cutover is locked.'
    conn = get_db()
    cur = get_cursor(conn)
    current_max = _current_bill_max(cur)
    ok, msg = validate_start_seq(start_seq, current_max)
    if not ok:
        conn.close()
        return False, msg
    cur.execute('''
        INSERT INTO cutover_seed (seed_type, doc_series, financial_year, start_seq, created_by, updated_by, updated_at)
        VALUES ('bill', '', '', %s, %s, %s, now())
        ON CONFLICT (seed_type, doc_series, financial_year)
        DO UPDATE SET start_seq=EXCLUDED.start_seq, updated_by=EXCLUDED.updated_by, updated_at=now()
    ''', [start_seq, username, username])
    conn.commit()
    conn.close()
    write_audit('set_bill_seed', {'start_seq': start_seq}, username)
    return True, ''


# ===== Mark / unmark items billed (pure flag, no invoice, no SAP) =====

def _apply_billed(cur, cargo_items, service_ids, billed):
    """Flip billed flags. cargo_items: list of {'source_type','id','bill_quantity'}.
    billed=True  -> billed_quantity += bill_quantity (capped at total),
                    is_billed=1 only once fully covered. A missing/<=0
                    bill_quantity marks the whole remaining balance.
    billed=False -> is_billed=0, billed_quantity=0 (full reset).
    Returns counts dict. Raises ValueError on unknown source_type."""
    cargo_done, svc_done = 0, 0
    for item in cargo_items or []:
        mapping = cargo_source(item.get('source_type'))
        if not mapping:
            raise ValueError(f"Unknown cargo source_type: {item.get('source_type')}")
        table, qty_col = mapping
        if billed:
            # qty_col and table are trusted constants from CARGO_SOURCES (never
            # user input). Read current totals to support partial marking.
            cur.execute(
                f"SELECT {qty_col} AS total, COALESCE(billed_quantity, 0) AS already "
                f"FROM {table} WHERE id=%s",
                [item.get('id')])
            row = cur.fetchone()
            if not row:
                continue
            new_billed, is_billed = compute_partial_billed(
                row['total'], row['already'], item.get('bill_quantity'))
            cur.execute(
                f"UPDATE {table} SET is_billed=%s, billed_quantity=%s WHERE id=%s",
                [is_billed, new_billed, item.get('id')])
        else:
            cur.execute(
                f"UPDATE {table} SET is_billed=0, billed_quantity=0 WHERE id=%s",
                [item.get('id')])
        cargo_done += cur.rowcount
    for sid in service_ids or []:
        if billed:
            cur.execute("UPDATE service_records SET is_billed=1 WHERE id=%s", [sid])
        else:
            cur.execute("UPDATE service_records SET is_billed=0, bill_id=NULL WHERE id=%s", [sid])
        svc_done += cur.rowcount
    return {'cargo': cargo_done, 'services': svc_done}


def mark_items_billed(cargo_items, service_ids, username, billed=True):
    """Mark (or unmark) the given items as billed. Pure status flag - no bill,
    no invoice, no SAP. Transactional. Returns (ok, message, counts)."""
    if is_locked():
        return False, 'Cutover is locked.', {}
    conn = get_db()
    cur = get_cursor(conn)
    try:
        counts = _apply_billed(cur, cargo_items, service_ids, billed)
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return False, str(e), {}
    conn.close()
    write_audit('mark_billed' if billed else 'unmark_billed',
                {'cargo': cargo_items, 'services': service_ids, 'counts': counts},
                username)
    return True, '', counts


def set_fdcn_seed(doc_series, financial_year, start_seq, username):
    """Upsert a credit/debit-note seed. Keyed like the invoice seed, on the
    FDCN01 series prefix (e.g. DPPLCN) and its FY format ('26-27').
    Returns (ok, message)."""
    if is_locked():
        return False, 'Cutover is locked.'
    conn = get_db()
    cur = get_cursor(conn)
    current_max = _current_fdcn_max(cur, doc_series, financial_year)
    ok, msg = validate_start_seq(start_seq, current_max)
    if not ok:
        conn.close()
        return False, msg
    cur.execute('''
        INSERT INTO cutover_seed (seed_type, doc_series, financial_year, start_seq, created_by, updated_by, updated_at)
        VALUES ('fdcn', %s, %s, %s, %s, %s, now())
        ON CONFLICT (seed_type, doc_series, financial_year)
        DO UPDATE SET start_seq=EXCLUDED.start_seq, updated_by=EXCLUDED.updated_by, updated_at=now()
    ''', [doc_series, financial_year, start_seq, username, username])
    conn.commit()
    conn.close()
    write_audit('set_fdcn_seed',
                {'doc_series': doc_series, 'financial_year': financial_year, 'start_seq': start_seq},
                username)
    return True, ''
