from database import get_db, get_cursor
from datetime import datetime


# ===== CUTOVER SEED HELPERS =====

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


def delete_bill(bill_id):
    """Delete bill header and all lines"""
    conn = get_db()
    cur = get_cursor(conn)
    # Reverse billed tracking on cargo declaration tables
    cur.execute('''
        SELECT cargo_source_type, cargo_source_id, quantity
        FROM bill_lines
        WHERE bill_id = %s AND cargo_source_type IS NOT NULL AND cargo_source_id IS NOT NULL
    ''', (bill_id,))
    for row in cur.fetchall():
        _unmark_cargo_source_billed(
            cur,
            row['cargo_source_type'],
            row['cargo_source_id'],
            float(row['quantity'] or 0)
        )
    # Unmark service records as billed
    cur.execute('''UPDATE service_records SET is_billed=0, bill_id=NULL
        WHERE bill_id IN (SELECT id FROM bill_header WHERE id=%s)''', (bill_id,))
    # Delete bill (cascades to lines)
    cur.execute('DELETE FROM bill_header WHERE id=%s', (bill_id,))
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

    conn.commit()
    conn.close()
    return invoice_id, invoice_number


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
