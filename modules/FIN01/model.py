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
    cur.execute('UPDATE service_records SET is_billed=0, bill_id=NULL WHERE bill_id = %s',
                [bill_id])


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
            cur.execute('UPDATE service_records SET is_billed=1, bill_id=%s WHERE id=%s',
                        [bill_id, row['service_record_id']])


def reconcile_rejected_bill_sources():
    """Startup self-heal: bills rejected before rejection started releasing
    cargo tracking still hold billed_quantity on their declarations, keeping
    them stuck as non-billable. Release them exactly once.

    Idempotent: only touches declaration rows whose bill_id still points at
    the rejected bill, and release_bill_sources clears that link afterwards.
    ponytail: a declaration partially billed by two bills where the ACTIVE
    bill marked last is skipped (bill_id points elsewhere) — safe direction,
    reject that bill again through the UI if it ever comes up."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT id FROM bill_header WHERE bill_status = 'Rejected'")
    for row in cur.fetchall():
        release_bill_sources(cur, row['id'], only_if_still_linked=True)
    conn.commit()
    conn.close()


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
        cur.execute('''UPDATE service_records SET is_billed=0, bill_id=NULL
            WHERE bill_id = %s''', [bill_id])
        cur.execute("UPDATE bill_header SET bill_status='Cancelled' WHERE id=%s", [bill_id])

    return [b['bill_number'] for b in bills]


# ===== BILL FUNCTIONS =====

def get_next_bill_number():
    """Generate next bill number, honouring a cutover bill seed as a floor."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(
        "SELECT MAX(CAST(SUBSTR(bill_number, 5) AS INTEGER)) FROM bill_header WHERE bill_number LIKE 'BILL%%'"
    )
    existing_max = (cur.fetchone()['max'] or 0)
    seed = lookup_seed(cur, 'bill')      # doc_series='', financial_year=''
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


def save_bill_header(data):
    """Save bill header"""
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')

    if row_id:
        cols = [k for k in data if k not in ['id', 'bill_number']]
        cur.execute(f'''UPDATE bill_header
            SET {', '.join([f'{c}=%s' for c in cols])}
            WHERE id=%s''',
            [data[c] for c in cols] + [row_id])
    else:
        data['bill_number'] = get_next_bill_number()
        cols = [k for k in data if k != 'id']
        cur.execute(f'''INSERT INTO bill_header
            ({', '.join(cols)})
            VALUES ({', '.join(['%s']*len(cols))})
            RETURNING id''',
            [data[c] for c in cols])
        row_id = cur.fetchone()['id']

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

    cur.execute('''SELECT DISTINCT service_record_id FROM bill_lines
                   WHERE bill_id=%s AND service_record_id IS NOT NULL''', [bill_id])
    for row in cur.fetchall():
        srid = row['service_record_id']
        other = _other_active_bill_for_service(cur, srid, bill_id)
        if other:
            cur.execute('UPDATE service_records SET is_billed=1, bill_id=%s WHERE id=%s', [other, srid])
        else:
            cur.execute('UPDATE service_records SET is_billed=0, bill_id=NULL WHERE id=%s', [srid])


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


def get_bill_lines(bill_id):
    """Get all lines for a bill"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM bill_lines WHERE bill_id = %s ORDER BY id', (bill_id,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_bill_line(data):
    """Save bill line (supports both EU lines and service records)"""
    conn = get_db()
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
        cur.execute('UPDATE service_records SET is_billed = 1, bill_id = %s WHERE id = %s',
                     [data.get('bill_id'), data.get('service_record_id')])

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
            cur.execute(
                'UPDATE service_records SET is_billed=0, bill_id=NULL WHERE id=%s',
                [bl['service_record_id']]
            )
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

    cur.execute('SELECT round_off FROM invoice_header WHERE id=%s', [invoice_id])
    r = cur.fetchone()
    round_off = float((r['round_off'] if r else 0) or 0)
    total = round(totals['subtotal'] + totals['cgst_amount'] + totals['sgst_amount']
                  + totals['igst_amount'] + round_off, 2)
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
