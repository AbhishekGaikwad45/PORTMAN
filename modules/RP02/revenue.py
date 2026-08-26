"""RP02 Revenue Register — shared constants + the backdated upload.

The register itself is built live from invoice_header / invoice_lines, so
anything invoiced before go-live has no rows there. Finance uploads those here
(CSV/Excel, same columns as the register grid) and the register reads both,
tagging the uploaded rows 'Backdated'. Mirrors the bill master upload pattern.
"""
from database import get_db, get_cursor
from .model import _blank, _norm_header, parse_number, read_matrix

TABLE = 'rp02_revenue_backdated'

# (stored field, header) — one source of truth for the stored columns, the CSV
# template and the header map. Order matches the Revenue Register grid.
# Days/Bucket are NOT here on purpose: they are recomputed from the date every
# time the register is read, so storing them would go stale overnight. The
# template still carries the two columns (the sheet finance keeps has them) and
# the parser ignores whatever is in them.
FIELDS = [
    ('invoice_no',     'Invoice No.'),
    ('group_type',     'Group/Non group'),
    ('revenue_type_1', 'Revenue Type 1'),
    ('revenue_type_2', 'Revenue Type 2'),
    ('cargo_volume',   'Cargo Volume'),
    ('invoice_date',   'Date'),
    ('cust_code',      'Cust Code'),
    ('customer_name',  'Customer Name'),
    ('gl_code',        'GL CODE'),
    ('grouping_label', 'Grouping'),
    ('qty',            'Qty - MT'),
    ('rate',           'Rate'),
    ('tax_category',   'Tax Category'),
    ('tax_rate',       'Tax Rate'),
    ('basic_value',    'Basic Value (Rs.)'),
    ('sgst',           'SGST (Rs.)'),
    ('cgst',           'CGST (Rs.)'),
    ('igst',           'IGST (Rs.)'),
    ('invoice_value',  'Invoice value (Rs.)'),
    ('gstin',          'GSTIN'),
    ('sap_doc_no',     'SAP Doc No'),
    ('sac_code',       'SAC CODE'),
    ('hsn_code',       'HSN CODE'),
    ('irn',            'IRN'),
    ('ack_date',       'Ack Date'),
    ('ack_no',         'Ack No'),
    ('barcode',        'BARCODE'),
    ('booking_status', 'BOOKING/PAYMENT STATUS'),
    ('tds_tcs',        'TDS/TCS'),
    ('net_receivable', 'NET RECEIVABLE'),
]

COLUMNS = [f for f, _ in FIELDS]
DERIVED_HEADERS = ['Days', 'Bucket']          # emitted in the template, ignored on upload
TEMPLATE_HEADERS = ['Sr'] + [h for _, h in FIELDS] + DERIVED_HEADERS
# Cargo Volume is absent on purpose: the register sheet carries YES/NO there.
NUMERIC = {'qty', 'rate', 'tax_rate', 'basic_value', 'sgst', 'cgst', 'igst',
           'invoice_value', 'tds_tcs', 'net_receivable'}


def _norm(h):
    return _norm_header(h).rstrip('.')


HEADER_MAP = {_norm(h): f for f, h in FIELDS}
HEADER_MAP.update({
    'invoice number':    'invoice_no',
    'bill no':           'invoice_no',
    'revenue type':      'revenue_type_1',   # a repeated header fills 1 then 2
    'invoice date':      'invoice_date',
    'customer code':     'cust_code',
    'sap customer code': 'cust_code',
    'qty':               'qty',
    'quantity':          'qty',
    'basic value':       'basic_value',
    'invoice value':     'invoice_value',
    'sac code':          'sac_code',
    'hsn code':          'hsn_code',
    'net receivable':    'net_receivable',
})

# GL code → TDS/TCS %. Used by the live register and to fill uploaded rows
# that leave TDS blank.
TDS_PERCENT = {
    '4101076010': 2.0,
    '4101076030': 2.0,
    '4201090080': 2.0,
    '4101076100': 2.0,
    '4101071000': 0.1,
    '4201090210': 10.0,
    '4101076020': 2.0,
}

_AGE_BUCKETS = ((30, '0-30 days'), (60, '31-60 days'), (90, '61-90 days'),
                (180, '91-180 days'), (365, '181-365 days'),
                (730, '1-2 years'), (1095, '2-3 years'))


def age_bucket(days):
    """Ageing bucket for an invoice `days` old. Blank or future-dated → ''."""
    if days is None or days == '' or days < 0:
        return ''
    for limit, label in _AGE_BUCKETS:
        if days <= limit:
            return label
    return '> 3 years'


_DATE_FORMATS = ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d.%m.%Y', '%Y/%m/%d',
                 '%d-%b-%Y', '%d-%b-%y', '%d %b %Y')


def parse_date(v):
    """Date cell → datetime.date. Blank → None. Unparseable → ValueError."""
    from datetime import datetime as _dt
    if _blank(v):
        return None
    if hasattr(v, 'year'):                    # openpyxl hands back date/datetime
        return v.date() if hasattr(v, 'hour') else v
    s = str(v).strip()
    for cand in (s, s[:10]):
        for fmt in _DATE_FORMATS:
            try:
                return _dt.strptime(cand, fmt).date()
            except ValueError:
                pass
    raise ValueError("invalid date '%s'" % v)


# ── Parsing ──────────────────────────────────────────────────────────────────
def _index(headers):
    """Field → column position. A repeated 'Revenue type' header fills 1 then 2."""
    idx = {}
    for i, h in enumerate(headers):
        f = HEADER_MAP.get(_norm(h))
        if f == 'revenue_type_1' and f in idx:
            f = 'revenue_type_2'
        if f and f not in idx:
            idx[f] = i
    return idx


def _fill_derived(rec):
    """Fill TDS/TCS and net receivable when the file leaves them blank."""
    if rec.get('tds_tcs') is None and rec.get('basic_value') is not None:
        pct = TDS_PERCENT.get((rec.get('gl_code') or '').strip(), 0)
        rec['tds_tcs'] = round(rec['basic_value'] * pct / 100, 2)
    if rec.get('net_receivable') is None and rec.get('invoice_value') is not None:
        rec['net_receivable'] = round(rec['invoice_value'] - (rec.get('tds_tcs') or 0), 2)


def parse_rows(headers, raw_rows):
    """Map raw spreadsheet rows to field dicts → (rows, errors). Fully blank
    rows are skipped. Required: Invoice No., Date, Customer Name. Row numbers
    are 1-based spreadsheet rows (header row included)."""
    idx = _index(headers)
    rows, errors = [], []

    def cell(r, key):
        i = idx.get(key)
        return r[i] if (i is not None and i < len(r)) else None

    for n, r in enumerate(raw_rows, start=2):   # +1 header, +1 to 1-base
        if all(_blank(c) for c in r):
            continue
        rec, row_errs = {}, []
        for key in COLUMNS:
            v = cell(r, key)
            try:
                if key in NUMERIC:
                    rec[key] = parse_number(v)
                elif key == 'invoice_date':
                    rec[key] = parse_date(v)
                else:
                    rec[key] = (str(v).strip() if not _blank(v) else None)
            except ValueError as e:
                rec[key] = None
                row_errs.append('%s: %s' % (dict(FIELDS).get(key, key), e))

        if not rec.get('invoice_no'):
            row_errs.append('Invoice No. is required')
        if rec.get('invoice_date') is None and _blank(cell(r, 'invoice_date')):
            row_errs.append('Date is required')
        if not rec.get('customer_name'):
            row_errs.append('Customer Name is required')

        if row_errs:
            errors.append({'row': n, 'message': '; '.join(row_errs)})
        else:
            _fill_derived(rec)
            rows.append(rec)
    return rows, errors


def parse_upload(file_storage):
    """Parse a backdated register upload (.csv/.xlsx) → (rows, errors). The
    header row is the first one carrying Invoice No. plus two more known
    columns, so title rows above it are tolerated."""
    matrix, errors = read_matrix(file_storage)
    if errors:
        return [], errors
    for i, row in enumerate(matrix):
        idx = _index([str(c if c is not None else '') for c in row])
        if 'invoice_no' in idx and len(idx) >= 3:
            headers = [str(c).strip() if c is not None else '' for c in row]
            return parse_rows(headers, matrix[i + 1:])
    return [], [{'row': 0, 'message': 'Could not find a header row — download the '
                                      'template and keep its column names'}]


TEMPLATE_EXAMPLE_ROWS = [
    ['1', 'DPPL/25-26/001', 'Non Group', 'Cargo Handling', 'Wharfage', 'YES',
     '05-04-2025', '10000123', 'JSW Steel Ltd', '4101076010', 'Cargo Handling Charges',
     '19500', '120.00', 'CGST+SGST', '18', '2340000.00', '210600.00', '210600.00',
     '0.00', '2761200.00', '27AAACJ4323N1ZS', '9100000123', '', '996751',
     '', '', '', '', 'Booked', '46800.00', '2714400.00', '', ''],
    ['2', 'DPPL/25-26/002', 'Group', 'Marine', 'Berth Hire', 'NO',
     '12-04-2025', '10000456', 'JSW Cement Ltd', '4101071000', 'Berth Hire Charges',
     '1', '85000.00', 'IGST', '18', '85000.00', '0.00', '0.00', '15300.00',
     '100300.00', '29AAACJ0616P1ZL', '9100000456', '', '996729',
     '', '', '', '', 'Pending', '85.00', '100215.00', '', ''],
]


def build_template_csv():
    """The backdated revenue register upload template as CSV text. Days and
    Bucket are there to match the register sheet; leave them blank."""
    import io, csv as _csv
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(TEMPLATE_HEADERS)
    for r in TEMPLATE_EXAMPLE_ROWS:
        w.writerow(r)
    return buf.getvalue()


# ── Storage ──────────────────────────────────────────────────────────────────
def replace_months(rows, uploaded_by):
    """Replace stored rows for every month present in the file, in one
    transaction. Other months are untouched. Returns (inserted, months)."""
    months = sorted({r['invoice_date'].strftime('%Y-%m') for r in rows})
    conn = get_db()
    cur = get_cursor(conn)
    try:
        if months:
            cur.execute("DELETE FROM %s WHERE to_char(invoice_date, 'YYYY-MM') = ANY(%%s)"
                        % TABLE, [months])
        cols = COLUMNS + ['uploaded_by']
        sql = 'INSERT INTO %s (%s) VALUES (%s)' % (
            TABLE, ', '.join(cols), ', '.join(['%s'] * len(cols)))
        for r in rows:
            cur.execute(sql, [r.get(c) for c in COLUMNS] + [uploaded_by])
        conn.commit()
        return len(rows), months
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_status():
    """{count, uploaded_at, months} for the stored backdated dataset."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('SELECT COUNT(*) AS c, MAX(uploaded_at) AS at FROM %s' % TABLE)
        r = cur.fetchone()
        cur.execute("SELECT DISTINCT to_char(invoice_date, 'YYYY-MM') AS m "
                    'FROM %s ORDER BY m' % TABLE)
        months = [x['m'] for x in cur.fetchall()]
        at = r['at']
        return {'count': r['c'], 'uploaded_at': at.isoformat() if at else None,
                'months': months}
    finally:
        conn.close()


def _row_out(d):
    """DB row → JSON-safe dict (numerics as float, dates as text)."""
    out = dict(d)
    for k in NUMERIC:
        if out.get(k) is not None:
            out[k] = float(out[k])
    if out.get('invoice_date') is not None:
        out['invoice_date'] = str(out['invoice_date'])[:10]
    if out.get('uploaded_at') is not None:
        out['uploaded_at'] = str(out['uploaded_at'])
    return out


def get_rows(page=1, size=50, filters=None):
    """Paginated stored rows. `filters` is a list of {field, value}; each is
    ILIKE-matched against the FULL dataset and AND-combined."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        allowed = set(COLUMNS)
        where_parts, params = [], []
        for f in (filters or []):
            field = f.get('field')
            val = (f.get('value') or '').strip() if f.get('value') is not None else ''
            if field in allowed and val:
                where_parts.append('%s::TEXT ILIKE %%s' % field)
                params.append('%' + val + '%')
        where = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''
        cur.execute('SELECT COUNT(*) AS c FROM %s %s' % (TABLE, where), params)
        total = cur.fetchone()['c']
        cur.execute('SELECT * FROM %s %s ORDER BY invoice_date DESC, id DESC '
                    'LIMIT %%s OFFSET %%s' % (TABLE, where),
                    params + [size, (page - 1) * size])
        return [_row_out(r) for r in cur.fetchall()], total
    finally:
        conn.close()


def update_row(row_id, data):
    """Validate + update one stored row. {'success': True} or {'error': msg}.
    Mirrors the upload validators."""
    clean = {}
    for k in COLUMNS:
        v = data.get(k)
        try:
            if k in NUMERIC:
                clean[k] = parse_number(v)
            elif k == 'invoice_date':
                clean[k] = parse_date(v)
            else:
                clean[k] = (str(v).strip() if v not in (None, '') else None)
        except ValueError as e:
            return {'error': '%s: %s' % (dict(FIELDS).get(k, k), e)}
    for req, label in (('invoice_no', 'Invoice No.'), ('invoice_date', 'Date'),
                       ('customer_name', 'Customer Name')):
        if not clean.get(req):
            return {'error': '%s is required' % label}

    conn = get_db()
    cur = get_cursor(conn)
    try:
        sets = ', '.join(['%s=%%s' % c for c in COLUMNS])
        cur.execute('UPDATE %s SET %s WHERE id=%%s' % (TABLE, sets),
                    [clean.get(c) for c in COLUMNS] + [row_id])
        conn.commit()
        return {'success': True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_row(row_id):
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('DELETE FROM %s WHERE id=%%s' % TABLE, [row_id])
        conn.commit()
    finally:
        conn.close()


def get_register_rows(month=None, year=None):
    """Stored backdated rows shaped like the live Revenue Register rows."""
    from datetime import date as _date
    where, params = [], []
    if month:
        where.append('EXTRACT(MONTH FROM invoice_date) = %s')
        params.append(int(month))
    if year:
        where.append('EXTRACT(YEAR FROM invoice_date) = %s')
        params.append(int(year))
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('SELECT * FROM %s %s ORDER BY invoice_date DESC, id DESC' % (
            TABLE, ('WHERE ' + ' AND '.join(where)) if where else ''), params)
        rows = cur.fetchall()
    finally:
        conn.close()

    today = _date.today()
    out = []
    for r in rows:
        d = _row_out(r)
        rec = {k: (d[k] if d[k] is not None else '') for k in COLUMNS}
        rec['grouping'] = rec.pop('grouping_label')
        rec['date'] = rec.pop('invoice_date')
        rec['irn_date'] = rec['ack_date']
        days = (today - r['invoice_date']).days
        rec['days'] = days
        rec['bucket'] = age_bucket(days)
        rec['source'] = 'Backdated'
        out.append(rec)
    return out
