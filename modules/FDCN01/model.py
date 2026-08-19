from database import get_db, get_cursor
from modules.FIN01.model import lookup_seed, next_from_seed
from datetime import datetime


# ---------------------------------------------------------------------------
# Financial Year helper
# ---------------------------------------------------------------------------
def get_financial_year(date_str):
    """Return FY as 'YY-YY' (Apr-Mar). e.g. 2026-01-15 → '25-26'."""
    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')
    date_str = str(date_str)[:10]
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        dt = datetime.now()
    year = dt.year
    month = dt.month
    if month < 4:
        start_year = year - 1
    else:
        start_year = year
    end_year = start_year + 1
    return f'{start_year % 100:02d}-{end_year % 100:02d}'


# ---------------------------------------------------------------------------
# Doc Number generation
# ---------------------------------------------------------------------------
# Fallback prefixes, used until a default series is configured in CNDS01.
# doc_number stays a Portbird-side identifier (list/print/dedup/remarks) — the
# SAP Reference comes from the original invoice, so the prefix is never a SAP
# field and changing it cannot disturb a posted document.
FDCN_DOC_PREFIXES = {'DN': 'DPPLDN', 'CN': 'DPPLCN'}


def normalize_prefix(prefix):
    """Tidy a stored series prefix: trimmed, uppercased, no wrapping slashes.

    get_next_doc_number appends '/{FY}/{seq}' itself, so a prefix must not carry
    its own outer separators — 'DPPLCN/' would yield 'DPPLCN//25-26/0001'."""
    return (prefix or '').strip().strip('/').upper()


def resolve_doc_prefix(configured_prefix, doc_type):
    """Pure prefix choice: the configured CNDS01 series if one is set, else the
    built-in fallback. Unknown doc types pass through unchanged."""
    return normalize_prefix(configured_prefix) or FDCN_DOC_PREFIXES.get(doc_type, doc_type)


def fdcn_doc_prefix(doc_type):
    """Doc-number prefix for a DN/CN: the CNDS01 default series for this type,
    else the built-in fallback."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('''SELECT prefix FROM fdcn_doc_series
                       WHERE type = %s AND is_default = TRUE
                       ORDER BY id LIMIT 1''', [doc_type])
        row = cur.fetchone()
    except Exception:
        # Table absent (nothing ever configured) — keep issuing numbers.
        conn.rollback()
        row = None
    finally:
        conn.close()
    return resolve_doc_prefix(row['prefix'] if row else '', doc_type)


def get_next_doc_number(doc_type, doc_date):
    """Generate next doc number: PREFIX/FY/SEQ  e.g. DPPLCN/25-26/0001."""
    prefix = fdcn_doc_prefix(doc_type)
    fy = get_financial_year(doc_date)

    conn = get_db()
    cur = get_cursor(conn)
    # Get max sequence for this prefix/FY
    cur.execute('''SELECT COALESCE(MAX(doc_series_seq), 0) AS max_seq
        FROM fdcn_header WHERE doc_series=%s AND financial_year=%s''', [prefix, fy])
    max_seq = cur.fetchone()['max_seq'] or 0
    # An admin cutover seed (ADMIN > Cutover) is a floor only: once real notes
    # pass it, normal incrementing takes over.
    next_seq = next_from_seed(max_seq, lookup_seed(cur, 'fdcn', prefix, fy))
    conn.close()

    doc_number = f'{prefix}/{fy}/{next_seq:04d}'
    return doc_number, prefix, next_seq, fy


# ---------------------------------------------------------------------------
# Header CRUD
# ---------------------------------------------------------------------------
def get_fdcn_list(page=1, size=20, status_filter=None, type_filter=None):
    conn = get_db()
    cur = get_cursor(conn)

    where_parts = []
    params = []
    if status_filter:
        where_parts.append('h.doc_status = %s')
        params.append(status_filter)
    if type_filter:
        where_parts.append('h.doc_type = %s')
        params.append(type_filter)

    where_sql = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

    cur.execute(f'SELECT COUNT(*) AS cnt FROM fdcn_header h {where_sql}', params)
    total = cur.fetchone()['cnt']

    cur.execute(f'''
        SELECT
            h.*,
            i.invoice_number AS original_invoice_number_display,
            ref.original_bill_numbers,
            ref.original_agreement_refs
        FROM fdcn_header h
        LEFT JOIN invoice_header i ON h.original_invoice_id = i.id
        LEFT JOIN LATERAL (
            SELECT
                STRING_AGG(DISTINCT ibm.bill_number, ', ') AS original_bill_numbers,
                STRING_AGG(
                    DISTINCT NULLIF(
                        TRIM(
                            COALESCE(ca.agreement_code, '') ||
                            CASE
                                WHEN COALESCE(ca.agreement_name, '') <> '' THEN ' - ' || ca.agreement_name
                                ELSE ''
                            END
                        ),
                        ''
                    ),
                    ', '
                ) AS original_agreement_refs
            FROM invoice_bill_mapping ibm
            LEFT JOIN bill_header bh ON ibm.bill_id = bh.id
            LEFT JOIN customer_agreements ca ON bh.agreement_id = ca.id
            WHERE ibm.invoice_id = h.original_invoice_id
        ) ref ON TRUE
        {where_sql}
        ORDER BY h.id DESC LIMIT %s OFFSET %s
    ''', params + [size, (page - 1) * size])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows, total


def get_fdcn_by_id(fdcn_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT
            h.*,
            i.invoice_number AS original_invoice_number_display,
            i.invoice_date AS original_invoice_date,
            ref.original_bill_numbers,
            ref.original_agreement_refs
        FROM fdcn_header h
        LEFT JOIN invoice_header i ON h.original_invoice_id = i.id
        LEFT JOIN LATERAL (
            SELECT
                STRING_AGG(DISTINCT ibm.bill_number, ', ') AS original_bill_numbers,
                STRING_AGG(
                    DISTINCT NULLIF(
                        TRIM(
                            COALESCE(ca.agreement_code, '') ||
                            CASE
                                WHEN COALESCE(ca.agreement_name, '') <> '' THEN ' - ' || ca.agreement_name
                                ELSE ''
                            END
                        ),
                        ''
                    ),
                    ', '
                ) AS original_agreement_refs
            FROM invoice_bill_mapping ibm
            LEFT JOIN bill_header bh ON ibm.bill_id = bh.id
            LEFT JOIN customer_agreements ca ON bh.agreement_id = ca.id
            WHERE ibm.invoice_id = h.original_invoice_id
        ) ref ON TRUE
        WHERE h.id = %s
    ''', [fdcn_id])
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def save_fdcn_header(data, username=None):
    conn = get_db()
    cur = get_cursor(conn)
    fdcn_id = data.get('id')
    now = datetime.now().strftime('%Y-%m-%d')

    if fdcn_id:
        cur.execute('''UPDATE fdcn_header SET
            doc_type=%s, doc_date=%s, original_invoice_id=%s, original_invoice_number=%s,
            customer_id=%s, customer_type=%s, customer_name=%s,
            customer_gstin=%s, customer_gst_state_code=%s, customer_gl_code=%s,
            subtotal=%s, cgst_amount=%s, sgst_amount=%s, igst_amount=%s, total_amount=%s,
            doc_status=%s, creation_type=%s, remarks=%s
            WHERE id=%s''', [
            data.get('doc_type'), data.get('doc_date'),
            data.get('original_invoice_id'), data.get('original_invoice_number'),
            data.get('customer_id'), data.get('customer_type'), data.get('customer_name'),
            data.get('customer_gstin'), data.get('customer_gst_state_code'),
            data.get('customer_gl_code'),
            data.get('subtotal', 0), data.get('cgst_amount', 0),
            data.get('sgst_amount', 0), data.get('igst_amount', 0),
            data.get('total_amount', 0),
            data.get('doc_status', 'Draft'),
            data.get('creation_type', 'rate_revision'),
            data.get('remarks'),
            fdcn_id
        ])
    else:
        doc_type = data.get('doc_type', 'DN')
        doc_date = data.get('doc_date', now)
        doc_number, prefix, seq, fy = get_next_doc_number(doc_type, doc_date)

        cur.execute('''INSERT INTO fdcn_header
            (doc_number, doc_type, doc_date, doc_series, doc_series_seq, financial_year,
             original_invoice_id, original_invoice_number,
             customer_id, customer_type, customer_name,
             customer_gstin, customer_gst_state_code, customer_gl_code,
             subtotal, cgst_amount, sgst_amount, igst_amount, total_amount,
             doc_status, creation_type, remarks, created_by, created_date)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id''', [
            doc_number, doc_type, doc_date, prefix, seq, fy,
            data.get('original_invoice_id'), data.get('original_invoice_number'),
            data.get('customer_id'), data.get('customer_type'), data.get('customer_name'),
            data.get('customer_gstin'), data.get('customer_gst_state_code'),
            data.get('customer_gl_code'),
            data.get('subtotal', 0), data.get('cgst_amount', 0),
            data.get('sgst_amount', 0), data.get('igst_amount', 0),
            data.get('total_amount', 0),
            data.get('doc_status', 'Draft'),
            data.get('creation_type', 'rate_revision'),
            data.get('remarks'),
            username, now
        ])
        fdcn_id = cur.fetchone()['id']

    conn.commit()
    conn.close()
    return fdcn_id


def delete_fdcn(fdcn_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('DELETE FROM fdcn_lines WHERE fdcn_id = %s', [fdcn_id])
    cur.execute('DELETE FROM fdcn_header WHERE id = %s', [fdcn_id])
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Lines CRUD
# ---------------------------------------------------------------------------
def get_fdcn_lines(fdcn_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''SELECT * FROM fdcn_lines WHERE fdcn_id = %s ORDER BY id''', [fdcn_id])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def save_fdcn_lines(fdcn_id, lines):
    """Replace all lines for a FDCN document."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('DELETE FROM fdcn_lines WHERE fdcn_id = %s', [fdcn_id])
    for line in lines:
        cur.execute('''INSERT INTO fdcn_lines
            (fdcn_id, invoice_line_id, service_type_id, service_name, service_description,
             quantity, uom, original_rate, revised_rate, rate_difference, line_amount,
             gst_rate_id, cgst_rate, sgst_rate, igst_rate,
             cgst_amount, sgst_amount, igst_amount, line_total,
             gl_code, sac_code, remarks)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', [
            fdcn_id,
            line.get('invoice_line_id'), line.get('service_type_id'),
            line.get('service_name'), line.get('service_description'),
            line.get('quantity', 0), line.get('uom'),
            line.get('original_rate', 0), line.get('revised_rate', 0),
            line.get('rate_difference', 0), line.get('line_amount', 0),
            line.get('gst_rate_id'), line.get('cgst_rate', 0),
            line.get('sgst_rate', 0), line.get('igst_rate', 0),
            line.get('cgst_amount', 0), line.get('sgst_amount', 0),
            line.get('igst_amount', 0), line.get('line_total', 0),
            line.get('gl_code'), line.get('sac_code'), line.get('remarks')
        ])
    conn.commit()
    conn.close()


def get_fdcn_sac_summary(fdcn_id):
    """Get SAC-wise summary for a DN/CN document."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT
            sac_code,
            SUM(line_amount) as taxable_value,
            SUM(cgst_amount) as cgst,
            SUM(sgst_amount) as sgst,
            SUM(igst_amount) as igst
        FROM fdcn_lines
        WHERE fdcn_id = %s
        GROUP BY sac_code
        ORDER BY sac_code
    ''', [fdcn_id])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Invoice lookups (for the entry form)
# ---------------------------------------------------------------------------
def get_invoices_for_customer(customer_type, customer_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT
            ih.id,
            ih.invoice_number,
            ih.invoice_date,
            ih.total_amount,
            ih.customer_name,
            ih.customer_gstin,
            ih.customer_gst_state_code,
            ih.customer_gl_code,
            map.bill_numbers,
            map.agreement_refs,
            cn.doc_number AS has_active_cn
        FROM invoice_header ih
        LEFT JOIN (
            SELECT
                ibm.invoice_id,
                STRING_AGG(DISTINCT ibm.bill_number, ', ') AS bill_numbers,
                STRING_AGG(
                    DISTINCT NULLIF(
                        TRIM(
                            COALESCE(ca.agreement_code, '') ||
                            CASE
                                WHEN COALESCE(ca.agreement_name, '') <> '' THEN ' - ' || ca.agreement_name
                                ELSE ''
                            END
                        ),
                        ''
                    ),
                    ', '
                ) AS agreement_refs
            FROM invoice_bill_mapping ibm
            LEFT JOIN bill_header bh ON ibm.bill_id = bh.id
            LEFT JOIN customer_agreements ca ON bh.agreement_id = ca.id
            GROUP BY ibm.invoice_id
        ) map ON map.invoice_id = ih.id
        LEFT JOIN LATERAL (
            SELECT doc_number FROM fdcn_header
            WHERE original_invoice_id = ih.id
              AND doc_type = 'CN'
              AND doc_status NOT IN ('Rejected', 'Cancelled')
            ORDER BY id DESC
            LIMIT 1
        ) cn ON TRUE
        WHERE ih.customer_type = %s AND ih.customer_id = %s
          AND ih.invoice_status NOT IN ('Cancelled')
        ORDER BY ih.id DESC
    ''', [customer_type, int(customer_id)])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_invoice_lines_for_fdcn(invoice_ids):
    """Get invoice lines with service_type_id (via bill_lines) for rate revision.
    Accepts a single int or list of ints."""
    if isinstance(invoice_ids, (int, str)):
        invoice_ids = [int(invoice_ids)]
    else:
        invoice_ids = [int(x) for x in invoice_ids]

    conn = get_db()
    cur = get_cursor(conn)
    placeholders = ','.join(['%s'] * len(invoice_ids))
    cur.execute(f'''
        SELECT il.id AS invoice_line_id,
               il.invoice_id,
               ih.invoice_number,
               il.service_name, il.service_description,
               il.quantity, il.uom, il.rate AS original_rate,
               il.line_amount, il.gl_code, il.sac_code,
               il.cgst_rate, il.sgst_rate, il.igst_rate,
               bl.service_type_id,
               CASE
                   WHEN bl.cargo_source_type IS NOT NULL THEN
                       COALESCE(imp.cargo_name, exp.cargo_name, mbc.cargo_name, bl.service_description)
                   ELSE NULL
               END AS cargo_name,
               CASE WHEN fl.id IS NOT NULL THEN fh.doc_number ELSE NULL END AS existing_fdcn_doc
        FROM invoice_lines il
        JOIN invoice_header ih ON il.invoice_id = ih.id
        LEFT JOIN bill_lines bl ON il.bill_id = bl.bill_id
            AND il.service_name = bl.service_name
            AND il.rate = bl.rate
            AND il.quantity = bl.quantity
            AND COALESCE(il.service_description, '') = COALESCE(bl.service_description, '')
        LEFT JOIN vcn_cargo_declaration imp
            ON bl.cargo_source_type = 'VCN_IMPORT' AND bl.cargo_source_id = imp.id
        LEFT JOIN vcn_export_cargo_declaration exp
            ON bl.cargo_source_type = 'VCN_EXPORT' AND bl.cargo_source_id = exp.id
        LEFT JOIN mbc_customer_details mbc
            ON bl.cargo_source_type = 'MBC' AND bl.cargo_source_id = mbc.id
        LEFT JOIN fdcn_lines fl ON fl.invoice_line_id = il.id
        LEFT JOIN fdcn_header fh ON fl.fdcn_id = fh.id
            AND fh.doc_status NOT IN ('Rejected', 'Cancelled')
        WHERE il.invoice_id IN ({placeholders})
        ORDER BY il.invoice_id, il.id
    ''', invoice_ids)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_customer_agreements(customer_type, customer_id):
    """Get active approved agreements for a customer."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT id, agreement_code, agreement_name, valid_from, valid_to, currency_code
        FROM customer_agreements
        WHERE customer_type = %s AND customer_id = %s
          AND agreement_status = 'Approved' AND is_active = 1
        ORDER BY valid_from DESC
    ''', [customer_type, int(customer_id)])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_agreement_rates(agreement_id):
    """Get all rate lines for an agreement, keyed by service_type_id (and cargo_id for cargo handling)."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT cal.service_type_id, cal.service_name, cal.rate, cal.uom, cal.currency_code,
               cal.cargo_id, cal.cargo_name,
               fst.service_name AS fst_service_name, fst.service_code
        FROM customer_agreement_lines cal
        LEFT JOIN finance_service_types fst ON cal.service_type_id = fst.id
        WHERE cal.agreement_id = %s
    ''', [int(agreement_id)])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_customers_for_billing(customer_type):
    """Get list of customers or agents for dropdown."""
    conn = get_db()
    cur = get_cursor(conn)
    if customer_type == 'Agent':
        cur.execute('''SELECT id, name, gstin, gst_state_code, gl_code
            FROM vessel_agents ORDER BY name''')
    else:
        cur.execute('''SELECT id, name, gstin, gst_state_code, gl_code
            FROM vessel_customers ORDER BY name''')
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Approval helpers
# ---------------------------------------------------------------------------
def update_fdcn_status(fdcn_id, status, username=None, rejection_reason=None):
    conn = get_db()
    cur = get_cursor(conn)
    now = datetime.now().strftime('%Y-%m-%d')
    if status == 'Approved':
        cur.execute('''UPDATE fdcn_header SET doc_status=%s, approved_by=%s, approved_date=%s
            WHERE id=%s''', [status, username, now, fdcn_id])
    elif status == 'Rejected':
        cur.execute('''UPDATE fdcn_header SET doc_status=%s, rejection_reason=%s
            WHERE id=%s''', [status, rejection_reason, fdcn_id])
    else:
        cur.execute('UPDATE fdcn_header SET doc_status=%s WHERE id=%s', [status, fdcn_id])
    conn.commit()
    conn.close()


def update_sap_details(fdcn_id, sap_doc_number, username):
    conn = get_db()
    cur = get_cursor(conn)
    now = datetime.now().strftime('%Y-%m-%d')
    cur.execute('''UPDATE fdcn_header
        SET sap_document_number=%s, sap_posting_date=%s,
            doc_status='Posted to SAP', posted_by=%s, posted_date=%s
        WHERE id=%s''', [sap_doc_number, now, username, now, fdcn_id])
    conn.commit()
    conn.close()


def create_cancellation_cn(invoice_id, username=None):
    """
    Create a full cancellation Credit Note for an invoice.
    Used when FB08 24hr reversal window has passed.
    Copies all invoice lines with original_rate = invoice rate, revised_rate = 0,
    so rate_difference = -rate and line_amount = -(qty * rate).
    """
    from modules.FIN01 import model as fin_model

    invoice = fin_model.get_invoice_by_id(invoice_id)
    if not invoice:
        raise ValueError('Invoice not found')

    invoice_lines = fin_model.get_invoice_lines(invoice_id)
    if not invoice_lines:
        raise ValueError('Invoice has no lines')

    now = datetime.now().strftime('%Y-%m-%d')
    doc_number, prefix, seq, fy = get_next_doc_number('CN', now)

    # Calculate totals (negate the invoice amounts)
    subtotal = 0
    cgst_total = 0
    sgst_total = 0
    igst_total = 0

    cn_lines = []
    for line in invoice_lines:
        qty = float(line.get('quantity') or 0)
        rate = float(line.get('rate') or 0)
        line_amount = round(qty * rate, 2)  # positive — CN module handles sign

        cgst_amt = float(line.get('cgst_amount') or 0)
        sgst_amt = float(line.get('sgst_amount') or 0)
        igst_amt = float(line.get('igst_amount') or 0)
        line_total = round(line_amount + cgst_amt + sgst_amt + igst_amt, 2)

        subtotal += line_amount
        cgst_total += cgst_amt
        sgst_total += sgst_amt
        igst_total += igst_amt

        cn_lines.append({
            'invoice_line_id': line.get('id'),
            'service_type_id': line.get('service_type_id'),
            'service_name': line.get('service_name'),
            'service_description': line.get('service_description') or line.get('service_name'),
            'quantity': qty,
            'uom': line.get('uom'),
            'original_rate': rate,
            'revised_rate': 0,
            'rate_difference': -rate,
            'line_amount': line_amount,
            'gst_rate_id': line.get('gst_rate_id'),
            'cgst_rate': float(line.get('cgst_rate') or 0),
            'sgst_rate': float(line.get('sgst_rate') or 0),
            'igst_rate': float(line.get('igst_rate') or 0),
            'cgst_amount': cgst_amt,
            'sgst_amount': sgst_amt,
            'igst_amount': igst_amt,
            'line_total': line_total,
            'gl_code': line.get('gl_code'),
            'sac_code': line.get('sac_code'),
            'remarks': f'Full cancellation of {invoice.get("invoice_number", "")}'
        })

    total_amount = round(subtotal + cgst_total + sgst_total + igst_total, 2)

    conn = get_db()
    cur = get_cursor(conn)

    cur.execute('''INSERT INTO fdcn_header
        (doc_number, doc_type, doc_date, doc_series, doc_series_seq, financial_year,
         original_invoice_id, original_invoice_number,
         customer_id, customer_type, customer_name,
         customer_gstin, customer_gst_state_code, customer_gl_code,
         subtotal, cgst_amount, sgst_amount, igst_amount, total_amount,
         doc_status, creation_type, remarks, created_by, created_date)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id''', [
        doc_number, 'CN', now, prefix, seq, fy,
        invoice_id, invoice.get('invoice_number'),
        invoice.get('customer_id'), invoice.get('customer_type'),
        invoice.get('customer_name'),
        invoice.get('customer_gstin'), invoice.get('customer_gst_state_code'),
        invoice.get('customer_gl_code'),
        subtotal, cgst_total, sgst_total, igst_total, total_amount,
        'Approved', 'cancellation',
        f'Full cancellation CN for invoice {invoice.get("invoice_number", "")}',
        username, now
    ])
    fdcn_id = cur.fetchone()['id']

    # Insert lines
    for line in cn_lines:
        cur.execute('''INSERT INTO fdcn_lines
            (fdcn_id, invoice_line_id, service_type_id, service_name, service_description,
             quantity, uom, original_rate, revised_rate, rate_difference, line_amount,
             gst_rate_id, cgst_rate, sgst_rate, igst_rate,
             cgst_amount, sgst_amount, igst_amount, line_total,
             gl_code, sac_code, remarks)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', [
            fdcn_id,
            line['invoice_line_id'], line['service_type_id'],
            line['service_name'], line['service_description'],
            line['quantity'], line['uom'],
            line['original_rate'], line['revised_rate'],
            line['rate_difference'], line['line_amount'],
            line['gst_rate_id'], line['cgst_rate'],
            line['sgst_rate'], line['igst_rate'],
            line['cgst_amount'], line['sgst_amount'],
            line['igst_amount'], line['line_total'],
            line['gl_code'], line['sac_code'], line['remarks']
        ])

    conn.commit()
    conn.close()
    return fdcn_id, doc_number


def update_gst_details(fdcn_id, irn, ack_number, ack_date, qr_code):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''UPDATE fdcn_header
        SET gst_irn=%s, gst_ack_number=%s, gst_ack_date=%s, gst_qr_code=%s,
            doc_status='Posted to GST'
        WHERE id=%s''', [irn, ack_number, ack_date, qr_code, fdcn_id])
    conn.commit()
    conn.close()
