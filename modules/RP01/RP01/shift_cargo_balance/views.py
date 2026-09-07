from flask import render_template, request, jsonify, session, redirect, url_for, Response
from functools import wraps
from datetime import date, datetime, timedelta
import io
import json
import re

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from .. import bp
from database import get_db, get_cursor

REPORT_CUTOFF_DATE = datetime(2026, 5, 1, 0, 0, 0)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def safe_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    value = str(value).strip()
    if not value or value.lower() in ('none', 'null', '—', '-', ''):
        return None

    try:
        if 'T' in value or len(value) == 10:
            return datetime.fromisoformat(value)
    except Exception:
        pass

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M"
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            pass

    return None


def _fmt_dt(val):
    if not val:
        return ''
    d = safe_dt(val)
    return d.strftime('%d-%m-%Y %H:%M') if d else ''


def _clean_cargo_name(raw):
    if not raw:
        return 'General Cargo'
    c = str(raw).strip()
    return re.sub(r'\s+', ' ', c)


def _clean_berth_name(raw):
    if not raw:
        return 'Jetty'
    v = str(raw).strip()
    u = v.upper()
    if not u or u in ('WAITING', 'NONE', 'NULL', '—', '-', 'EMPTY'):
        return 'Jetty'
    if 'WR' in u or 'R19' in u:
        return 'WR 19'
    m = re.search(r'\b(?:BERTH\s*(?:NO\.?)?\s*|B\s*-?\s*)?(\d+[A-Z]?)\b', u)
    if m:
        return f"BERTH {m.group(1)}"
    return v


def berth_sort_key(b_name):
    b = str(b_name).strip().upper()
    if 'JETTY' in b:
        return (0, 0, b)
    m = re.search(r'(\d+)([A-Z]?)', b)
    if m:
        num = int(m.group(1))
        suf = 0.5 if m.group(2) else 0.0
        return (1, num + suf, b)
    return (2, 999, b)


def _is_berth_10_to_12(berth_str):
    """Detect if berth is Berth 10, 11, or 12."""
    if not berth_str:
        return False, ''
    s = str(berth_str).strip().upper()
    m = re.search(r'\b(?:BERTH\s*(?:NO\.?)?\s*|B\s*-?\s*)?(10|11|12)\b', s)
    if m:
        return True, f"BERTH {m.group(1)}"
    return False, ''


def _fetch_shift_cargo_balance(report_date_str, shift_key):
    """
    Dynamically computes the cargo balance at the jetty for the given date and shift.
    Only includes barges and MBCs where alongside date & time exists.
    Separates Berth 10, 11, 12 into their own berth lines.
    Groups results cleanly by (berth, cargo).
    """
    shift_key = (shift_key or 'C').strip().upper()
    if shift_key not in ('A', 'B', 'C'):
        shift_key = 'C'

    try:
        target_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
    except Exception:
        target_date = datetime.now().date()
    target_date_str = target_date.strftime('%Y-%m-%d')

    if shift_key == 'A':
        start_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=6, minute=0, second=0)
        end_dt   = datetime.combine(target_date, datetime.min.time()).replace(hour=14, minute=0, second=0)
        shifts_up_to = ['A']
    elif shift_key == 'B':
        start_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=14, minute=0, second=0)
        end_dt   = datetime.combine(target_date, datetime.min.time()).replace(hour=22, minute=0, second=0)
        shifts_up_to = ['A', 'B']
    else:  # C Shift
        start_dt = datetime.combine(target_date, datetime.min.time()).replace(hour=22, minute=0, second=0)
        end_dt   = datetime.combine(target_date + timedelta(days=1), datetime.min.time()).replace(hour=6, minute=0, second=0)
        shifts_up_to = ['A', 'B', 'C']

    conn = get_db()
    cur  = get_cursor(conn)

    # ── 1. Barge Position Report (operator-saved berth layout for that shift) ──
    cur.execute("""
        SELECT berth_layout
        FROM barge_position_report
        WHERE report_date = %s::date AND shift = %s
        ORDER BY updated_at DESC
        LIMIT 1
    """, (target_date_str, shift_key))
    exact_bpr = cur.fetchone()

    carried_bpr = None
    if not exact_bpr:
        cur.execute("""
            SELECT berth_layout
            FROM barge_position_report
            WHERE report_date <= %s::date
            ORDER BY report_date DESC,
                     CASE shift WHEN 'C' THEN 3 WHEN 'B' THEN 2 WHEN 'A' THEN 1 ELSE 0 END DESC,
                     updated_at DESC
            LIMIT 1
        """, (target_date_str,))
        carried_bpr = cur.fetchone()

    bpr_row = exact_bpr or carried_bpr
    bpr_items = []
    if bpr_row and bpr_row['berth_layout']:
        layout = bpr_row['berth_layout']
        if isinstance(layout, str):
            try:
                layout = json.loads(layout)
            except Exception:
                layout = []
        if isinstance(layout, list):
            bpr_items = layout

    bpr_map = {}
    for it in bpr_items:
        name = (it.get('name') or '').strip().upper()
        if name:
            bpr_map[name] = it

    # ── 2. Live Barges: ONLY with along_side_berth ───────────────────────────
    cur.execute("""
        WITH discharge_sums AS (
            SELECT
                TRIM(UPPER(ll.barge_name)) AS barge_name,
                ll.source_id,
                SUM(COALESCE(ll.quantity, 0)) AS discharge_done_qty
            FROM lueu_lines ll
            WHERE ll.is_deleted IS NOT TRUE
              AND ll.source_type = 'VCN'
              AND (
                  TO_DATE(ll.entry_date, 'YYYY-MM-DD') < %s::date
                  OR (
                      TO_DATE(ll.entry_date, 'YYYY-MM-DD') = %s::date
                      AND UPPER(TRIM(ll.shift)) = ANY(%s)
                  )
              )
            GROUP BY TRIM(UPPER(ll.barge_name)), ll.source_id
        )
        SELECT
            l.id,
            l.barge_name,
            l.trip_number,
            h.vessel_name AS mother_vessel_name,
            h.vcn_id,
            l.cargo_name AS cargo_type,
            COALESCE(l.discharge_quantity, 0) AS qty_mt,
            COALESCE(ds.discharge_done_qty, 0) AS discharge_done_qty,
            l.along_side_berth,
            l.commence_discharge_berth,
            l.completed_discharge_berth,
            l.cast_off_berth,
            l.cast_off_berth_nt,
            l.cast_off_port
        FROM ldud_barge_lines l
        LEFT JOIN ldud_header h ON l.ldud_id = h.id
        LEFT JOIN discharge_sums ds
            ON ds.barge_name = TRIM(UPPER(CONCAT(l.barge_name, ' / ', COALESCE(l.trip_number::text, '1'))))
            AND ds.source_id = h.vcn_id
        WHERE l.barge_name IS NOT NULL
          AND TRIM(l.barge_name) <> ''
          AND l.along_side_berth IS NOT NULL
          AND TRIM(l.along_side_berth) <> ''
    """, (target_date_str, target_date_str, shifts_up_to))
    barge_rows = cur.fetchall()

    # ── 3. Live MBCs: ONLY with along_side_berth (vessel_all_made_fast) ─────
    cur.execute("""
        WITH mbc_discharge_sums AS (
            SELECT
                ll.source_id,
                SUM(COALESCE(ll.quantity, 0)) AS discharge_done_qty
            FROM lueu_lines ll
            WHERE ll.is_deleted IS NOT TRUE
              AND ll.source_type = 'MBC'
              AND (
                  TO_DATE(ll.entry_date, 'YYYY-MM-DD') < %s::date
                  OR (
                      TO_DATE(ll.entry_date, 'YYYY-MM-DD') = %s::date
                      AND UPPER(TRIM(ll.shift)) = ANY(%s)
                  )
              )
            GROUP BY ll.source_id
        )
        SELECT
            h.id,
            h.mbc_name,
            h.cargo_name AS cargo_type,
            COALESCE(h.bl_quantity, 0) AS qty_mt,
            COALESCE(ds.discharge_done_qty, 0) AS discharge_done_qty,
            COALESCE(dp.vessel_all_made_fast, elp.alongside_at_berth) AS along_side_berth,
            COALESCE(dp.vessel_cast_off, elp.cast_off_from_berth)     AS cast_off_berth,
            COALESCE(dp.vessel_unloading_berth, elp.berth_master)     AS berth
        FROM mbc_header h
        LEFT JOIN mbc_discharge_port_lines dp
            ON dp.mbc_id = h.id AND h.operation_type ILIKE 'Import'
        LEFT JOIN mbc_export_load_port_lines elp
            ON elp.mbc_id = h.id AND h.operation_type ILIKE 'Export'
        LEFT JOIN mbc_discharge_sums ds
            ON ds.source_id = h.id
        WHERE h.mbc_name IS NOT NULL
          AND TRIM(h.mbc_name) <> ''
          AND (
              (dp.vessel_all_made_fast IS NOT NULL AND TRIM(dp.vessel_all_made_fast) <> '')
              OR
              (elp.alongside_at_berth IS NOT NULL AND TRIM(elp.alongside_at_berth) <> '')
          )
    """, (target_date_str, target_date_str, shifts_up_to))
    mbc_rows = cur.fetchall()

    # ── 4. Fetch latest berth assignment per vessel from lueu_lines ────────
    cur.execute("""
        SELECT DISTINCT ON (TRIM(UPPER(barge_name)))
            TRIM(UPPER(barge_name)) AS bname,
            berth_name
        FROM lueu_lines
        WHERE berth_name IS NOT NULL AND TRIM(berth_name) <> ''
          AND is_deleted IS NOT TRUE
        ORDER BY TRIM(UPPER(barge_name)), id DESC
    """)
    lueu_berths = {r['bname']: r['berth_name'] for r in cur.fetchall()}

    cur.close()
    conn.close()

    candidates = []

    # ── Process Barges (Strict: only those with alongside date & time) ────
    for row in barge_rows:
        bname = (row['barge_name'] or '').strip()
        bname_u = bname.upper()
        arr_dt = safe_dt(row['along_side_berth'])
        dep_dt = safe_dt(row['cast_off_berth']) or safe_dt(row['cast_off_berth_nt']) or safe_dt(row['cast_off_port'])

        # Must have valid alongside date & time
        if not arr_dt or arr_dt < REPORT_CUTOFF_DATE or arr_dt > end_dt:
            continue
        if dep_dt and dep_dt < start_dt:
            continue

        tot = float(row['qty_mt'] or 0)
        done = float(row['discharge_done_qty'] or 0)
        bal = max(0.0, tot - done)
        if bal <= 0.01:
            continue

        # Resolve berth: BPR layout -> LUEU discharge -> 'Jetty'
        bpr_berth = bpr_map.get(bname_u, {}).get('berth')
        resolved_berth = bpr_berth or lueu_berths.get(bname_u) or 'Jetty'
        clean_b = _clean_berth_name(resolved_berth)

        cargo = _clean_cargo_name(row['cargo_type'])
        candidates.append({
            'vessel_name': bname,
            'berth': clean_b,
            'cargo': cargo,
            'balance': bal
        })

    # ── Process MBCs (Strict: only those with alongside date & time) ──────
    for row in mbc_rows:
        mname = (row['mbc_name'] or '').strip()
        mname_u = mname.upper()
        arr_dt = safe_dt(row['along_side_berth'])
        dep_dt = safe_dt(row['cast_off_berth'])

        # Must have valid alongside date & time
        if not arr_dt or arr_dt < REPORT_CUTOFF_DATE or arr_dt > end_dt:
            continue
        if dep_dt and dep_dt < start_dt:
            continue

        tot = float(row['qty_mt'] or 0)
        done = float(row['discharge_done_qty'] or 0)
        bal = max(0.0, tot - done)
        if bal <= 0.01:
            continue

        bpr_berth = bpr_map.get(mname_u, {}).get('berth')
        resolved_berth = row['berth'] or bpr_berth or lueu_berths.get(mname_u) or 'Jetty'
        clean_b = _clean_berth_name(resolved_berth)

        cargo = _clean_cargo_name(row['cargo_type'])
        candidates.append({
            'vessel_name': mname,
            'berth': clean_b,
            'cargo': cargo,
            'balance': bal
        })

    # ── Group by (berth, cargo) ────────────────────────────────────────────
    grouped = {}
    for c in candidates:
        key = (c['berth'], c['cargo'])
        if key not in grouped:
            grouped[key] = {
                'berth': c['berth'],
                'cargo': c['cargo'],
                'balance': 0.0,
                'vessels': []
            }
        grouped[key]['balance'] += c['balance']
        if c['vessel_name'] not in grouped[key]['vessels']:
            grouped[key]['vessels'].append(c['vessel_name'])

    table_items = []
    grand_total = 0.0
    sorted_items = sorted(
        grouped.values(),
        key=lambda x: (berth_sort_key(x['berth']), x['cargo'])
    )

    for it in sorted_items:
        r_bal = int(round(it['balance']))
        if r_bal <= 0:
            continue
        grand_total += r_bal
        is_sp, _ = _is_berth_10_to_12(it['berth'])
        table_items.append({
            'berth': it['berth'],
            'cargo': it['cargo'],
            'balance': r_bal,
            'is_special': is_sp,
            'berth_note': f"({it['berth']})" if is_sp else ''
        })

    grand_total = int(round(grand_total))

    # ── Build exact SMS block ─────────────────────────────────────────────
    sms_lines = [f"Cargo Balance at Jetty for shift {shift_key}", ""]
    if table_items:
        max_c_len = max(len(it['cargo']) for it in table_items)
        max_c_len = max(max_c_len, 16)
        for it in table_items:
            c_str = it['cargo'].ljust(max_c_len)
            b_str = f"{it['balance']:,} MT."
            if it['is_special'] and it['berth_note']:
                sms_lines.append(f"{c_str} : {b_str} {it['berth_note']}")
            else:
                sms_lines.append(f"{c_str} : {b_str}")

        sms_lines.append("(Note- If Any Loaded MBC Wt at Berth 10 to 12 then put Cargo separate)")
        sms_lines.append("")
        sms_lines.append(f"Total: {grand_total:,} MT.")
    else:
        sms_lines.append("No active cargo balance at jetty for this shift.")
        sms_lines.append("")
        sms_lines.append("Total: 0 MT.")

    sms_lines.append("")
    sms_lines.append("Regards")
    sms_text = "\n".join(sms_lines)

    return {
        'entry_date': target_date_str,
        'shift': shift_key,
        'shift_display': f"{shift_key} Shift",
        'title': f"Cargo Balance at Jetty for shift {shift_key}",
        'items': table_items,
        'total_qty': grand_total,
        'total_balance': grand_total,
        'sms_text': sms_text
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@bp.route('/module/RP01/shift-cargo-balance/')
@login_required
def shift_cargo_balance_index():
    today_str = datetime.now().strftime('%Y-%m-%d')
    return render_template(
        'shift_cargo_balance/shift_cargo_balance.html',
        username=session.get('username'),
        default_date=today_str,
        default_shift='C'
    )


@bp.route('/api/module/RP01/shift-cargo-balance/preview')
@bp.route('/api/module/RP01/shift-cargo-balance/data')
@login_required
def shift_cargo_balance_data():
    entry_date = (request.args.get('entry_date') or request.args.get('date') or '').strip()
    shift = (request.args.get('shift') or '').strip().upper()

    if not entry_date:
        entry_date = datetime.now().strftime('%Y-%m-%d')
    if shift not in ('A', 'B', 'C'):
        shift = 'C'

    data = _fetch_shift_cargo_balance(entry_date, shift)
    return jsonify(data)


@bp.route('/api/module/RP01/shift-cargo-balance/download')
@login_required
def shift_cargo_balance_download():
    entry_date = (request.args.get('entry_date') or request.args.get('date') or '').strip()
    shift = (request.args.get('shift') or '').strip().upper()

    if not entry_date:
        entry_date = datetime.now().strftime('%Y-%m-%d')
    if shift not in ('A', 'B', 'C'):
        shift = 'C'

    data = _fetch_shift_cargo_balance(entry_date, shift)

    # ── Build Excel matching exact format: berth, cargo, balance ──
    wb = Workbook()
    ws = wb.active
    ws.title = f"Shift {shift} Balance"
    ws.views.sheetView[0].showGridLines = True

    _thin = Side(style='thin', color='000000')
    border = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
    font_title = Font(name='Calibri', size=11, bold=True)
    font_header = Font(name='Calibri', size=11, bold=True)
    font_data = Font(name='Calibri', size=11)
    font_bold = Font(name='Calibri', size=11, bold=True)
    font_italic = Font(name='Calibri', size=9, italic=True)

    # Row 1: Title (Cargo Balance at Jetty for shift <shift>)
    ws.cell(1, 1, f"Cargo Balance at Jetty for shift {shift}").font = font_title

    # Row 2: Headers (BERTH, CARGO, BALANCE)
    headers = ['BERTH', 'CARGO', 'BALANCE']
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(2, ci, h)
        cell.font = font_header
        cell.border = border
        cell.alignment = Alignment(
            horizontal='center' if ci == 1 else ('right' if ci == 3 else 'left')
        )

    # Data Rows
    row_idx = 3
    for it in data['items']:
        c_berth = ws.cell(row_idx, 1, it['berth'])
        c_cargo = ws.cell(row_idx, 2, it['cargo'])
        c_bal = ws.cell(row_idx, 3, it['balance'])

        c_berth.font = font_data
        c_berth.border = border
        c_berth.alignment = Alignment(horizontal='left')

        c_cargo.font = font_data
        c_cargo.border = border
        c_cargo.alignment = Alignment(horizontal='left')

        c_bal.font = font_data
        c_bal.border = border
        c_bal.number_format = '#,##0'
        c_bal.alignment = Alignment(horizontal='right')

        row_idx += 1

    # Total Row
    c_tot_lbl = ws.cell(row_idx, 1, 'Total')
    c_tot_lbl.font = font_bold
    c_tot_lbl.border = border

    c_empty = ws.cell(row_idx, 2, '')
    c_empty.font = font_bold
    c_empty.border = border

    c_tot_val = ws.cell(row_idx, 3, data['total_qty'])
    c_tot_val.font = font_bold
    c_tot_val.border = border
    c_tot_val.number_format = '#,##0'
    c_tot_val.alignment = Alignment(horizontal='right')

    row_idx += 2
    ws.cell(row_idx, 1, "(Note- If Any Loaded MBC Wt at Berth 10 to 12 then put Cargo separate)").font = font_italic

    # Set column widths
    ws.column_dimensions['A'].width = 18
    ws.column_dimensions['B'].width = 32
    ws.column_dimensions['C'].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"Cargo_Balance_Jetty_{entry_date}_Shift_{shift}.xlsx"
    return Response(
        buf.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )
