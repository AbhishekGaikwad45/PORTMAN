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
from ..Barge_Position_Report.views import _fetch_all_barges

REPORT_CUTOFF_DATE = date(2026, 9, 1)
REPORT_CUTOFF_DT = datetime(2026, 9, 1, 0, 0, 0)


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


def _is_wr19(berth_str):
    if not berth_str:
        return False
    u = str(berth_str).strip().upper()
    if '-' in u and ('T' in u or ':' in u):
        return False
    return ('WR' in u and '19' in u) or u in ('WR 19', 'WR19', 'R19', 'R-19', 'WR-19', 'WT @ R19', 'WT R19')


def _is_berth_10_to_12(berth_str):
    if not berth_str:
        return False, ''
    s = str(berth_str).strip().upper()
    if '-' in s and ('T' in s or ':' in s):
        return False, ''
    m = re.search(r'\b(?:BERTH\s*(?:NO\.?)?\s*|B\s*-?\s*)(10|11|12)\b', s)
    if m:
        return True, m.group(1)
    if s in ('10', '11', '12'):
        return True, s
    return False, ''


def _is_mbc_vessel(item, live=None, mbc_names_set=None):
    """
    Identifies whether a vessel record is an MBC (Multi-Bulk Carrier) vs a Barge.
    Checks explicit 'type', name prefix, and known MBC master/header records.
    """
    itype = str((item.get('type') or (live.get('type') if live else '')) or '').strip().upper()
    if itype == 'MBC':
        return True
    name = (item.get('name') or (live.get('name') if live else '') or '').strip().upper()
    if name.startswith('MBC ') or name.startswith('MBC-') or name.startswith('M.B.C.'):
        return True
    if mbc_names_set and name in mbc_names_set:
        return True
    return False


SHIFT_RANK = {'A': 1, 'B': 2, 'C': 3}


def _is_upcoming_shift(target_date, shift_key, now=None):
    now = now or datetime.now()
    cur_op_date = _operational_date(now)
    cur_shift = _current_shift_code(now)
    if target_date > cur_op_date:
        return True
    if target_date == cur_op_date:
        return SHIFT_RANK.get(shift_key, 0) > SHIFT_RANK.get(cur_shift, 0)
    return False


def _fetch_shift_cargo_balance(report_date_str, shift_key):
    """
    Dynamically computes the cargo balance at the jetty for the given date and shift:
    - Cutoff Date: 01-07-2026. Dates before this are blocked.
    - Upcoming shift data is blocked and not shown.
    - Previous and current shift data are accurately fetched date-wise and shift-wise.
    - If viewing the current shift, real-time balances are synced live.
    - If viewing previous shifts, saved historical records for that shift/date are preserved.
    - WR 19: If available at that time, shown separately as Cargo : Balance MT. (WR 19).
    - Berth 10, 11, and 12: Show ONLY MBC cargo balance (not barges), as Cargo : Balance MT. (Berth No.X).
    - Other berths (Berths 1-9 / Jetty): Show all barges or MBC cargo balance aggregated by cargo name.
    """
    shift_key = (shift_key or 'C').strip().upper()
    if shift_key not in ('A', 'B', 'C'):
        shift_key = 'C'

    try:
        target_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
    except Exception:
        target_date = datetime.now().date()
    target_date_str = target_date.strftime('%Y-%m-%d')

    # Cutoff date validation (01-07-2026)
    if target_date < REPORT_CUTOFF_DATE:
        return {
            'entry_date': target_date_str,
            'shift': shift_key,
            'shift_display': f"{shift_key} Shift",
            'title': f"Cargo Balance at Jetty for {shift_key} Shift",
            'items': [],
            'total_qty': 0,
            'total_balance': 0,
            'is_cutoff': True,
            'sms_text': (
                f"Cargo Balance at Jetty for {shift_key} Shift\n\n"
                "Cutoff Date: 01-07-2026.\n"
                "Reports before this date are not available.\n\n"
                "Total: 0 MT.\n\n"
                "Regards"
            )
        }

    now = datetime.now()
    cur_op_date = _operational_date(now)
    cur_shift = _current_shift_code(now)

    is_upcoming = _is_upcoming_shift(target_date, shift_key, now)
    is_current = (target_date == cur_op_date and shift_key == cur_shift)

    # If shift is in the future, do not show any upcoming data
    if is_upcoming:
        return {
            'entry_date': target_date_str,
            'shift': shift_key,
            'shift_display': f"{shift_key} Shift",
            'title': f"Cargo Balance at Jetty for {shift_key} Shift",
            'items': [],
            'total_qty': 0,
            'total_balance': 0,
            'sms_text': (
                f"Cargo Balance at Jetty for {shift_key} Shift\n\n"
                "No balance data for upcoming shift.\n\n"
                "Total: 0 MT.\n\n"
                "Regards"
            )
        }

    conn = get_db()
    cur = get_cursor(conn)

    # Fetch known MBC names from mbc_header and mbc_master for 100% accurate MBC identification
    mbc_names_set = set()
    try:
        cur.execute("""
            SELECT DISTINCT UPPER(TRIM(mbc_name)) AS name FROM mbc_header WHERE mbc_name IS NOT NULL
            UNION
            SELECT DISTINCT UPPER(TRIM(mbc_name)) AS name FROM mbc_master WHERE mbc_name IS NOT NULL
        """)
        mbc_names_set = {r['name'] for r in cur.fetchall() if r.get('name')}
    except Exception:
        pass

    cutoff_date_str = REPORT_CUTOFF_DATE.strftime('%Y-%m-%d')

    # 1. Fetch exact or carried BPR (strictly on or before requested date and shift, not before cutoff date)
    cur.execute("""
        SELECT berth_layout, shift, report_date
        FROM barge_position_report
        WHERE report_date = %s::date
          AND report_date >= %s::date
          AND shift = %s
        ORDER BY updated_at DESC
        LIMIT 1
    """, (target_date_str, cutoff_date_str, shift_key))
    bpr_row = cur.fetchone()

    if not bpr_row:
        cur.execute("""
            SELECT berth_layout, shift, report_date
            FROM barge_position_report
            WHERE report_date >= %s::date
              AND (
                  report_date < %s::date
                  OR (
                      report_date = %s::date AND (
                          CASE shift WHEN 'C' THEN 3 WHEN 'B' THEN 2 WHEN 'A' THEN 1 ELSE 0 END <= %s
                      )
                  )
              )
            ORDER BY report_date DESC,
                     CASE shift WHEN 'C' THEN 3 WHEN 'B' THEN 2 WHEN 'A' THEN 1 ELSE 0 END DESC,
                     updated_at DESC
            LIMIT 1
        """, (cutoff_date_str, target_date_str, target_date_str, SHIFT_RANK.get(shift_key, 3)))
        bpr_row = cur.fetchone()

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

    # 2. Live barges & MBCs (used to sync real-time balances for the current ongoing shift)
    live_barges = []
    if is_current:
        try:
            live_barges, _ = _fetch_all_barges()
        except Exception:
            live_barges = []

    def find_live(item):
        i_id = str(item.get('id') or '').strip()
        i_name = (item.get('name') or '').strip().upper()

        if i_id:
            for b in live_barges:
                if str(b.get('id') or '').strip() == i_id:
                    return b

        matches = [b for b in live_barges if (b.get('name') or '').strip().upper() == i_name]
        if not matches:
            return None

        # Prioritize Under Discharge -> Waiting -> Discharge Completed
        for m in matches:
            if m.get('status') == 'Under Discharge':
                return m
        for m in matches:
            if m.get('status') == 'Waiting':
                return m
        return matches[0]

    # 3. Lookup latest berth assignments from lueu_lines for fallback (strictly on or after cutoff)
    lueu_berths = {}
    if is_current:
        cur.execute("""
            SELECT DISTINCT ON (TRIM(UPPER(barge_name)))
                TRIM(UPPER(barge_name)) AS bname,
                berth_name
            FROM lueu_lines
            WHERE berth_name IS NOT NULL AND TRIM(berth_name) <> ''
              AND is_deleted IS NOT TRUE
              AND (entry_date >= %s OR entry_date IS NULL)
            ORDER BY TRIM(UPPER(barge_name)), id DESC
        """, (cutoff_date_str,))
        lueu_berths = {r['bname']: r['berth_name'] for r in cur.fetchall()}

    cur.close()
    conn.close()

    candidates = []
    seen_ids = set()
    seen_names = set()

    for it in bpr_items:
        name = (it.get('name') or '').strip()
        if not name:
            continue
        berth_raw = str(it.get('berth') or '').strip().upper()

        # Skip waiting area (only WR 19 or berths are considered)
        if berth_raw in ('WAITING', 'NONE', 'NULL', '—', '-', 'EMPTY', ''):
            continue

        bal = float(it.get('balance') or it.get('balance_qty') or 0)
        cargo = _clean_cargo_name(it.get('cargo'))

        live = find_live(it) if is_current else None
        # Only sync with live data if currently viewing the active ongoing shift
        if is_current and live:
            bal = float(live.get('balance_qty') or 0)
            if live.get('cargo'):
                cargo = _clean_cargo_name(live.get('cargo'))

        if bal <= 0.01:
            continue

        is_mbc = _is_mbc_vessel(it, live=live, mbc_names_set=mbc_names_set)

        candidates.append({
            'id': str(it.get('id') or ''),
            'name': name,
            'cargo': cargo,
            'berth': berth_raw,
            'balance': bal,
            'is_mbc': is_mbc
        })
        if it.get('id'):
            seen_ids.add(str(it.get('id')))
        seen_names.add(name.upper())

    # For current shift: check live vessels not in BPR that are actively at berths
    if is_current:
        for b in live_barges:
            b_id = str(b.get('id') or '')
            b_name_u = (b.get('name') or '').strip().upper()
            if (b_id and b_id in seen_ids) or (b_name_u in seen_names):
                continue

            bal = float(b.get('balance_qty') or 0)
            if bal <= 0.01:
                continue

            assigned_berth = lueu_berths.get(b_name_u) or b.get('berth') or ''
            berth_raw = str(assigned_berth).strip().upper()
            if '-' in berth_raw and ('T' in berth_raw or ':' in berth_raw):
                berth_raw = 'Jetty'

            if not berth_raw or berth_raw in ('WAITING', 'NONE', 'NULL', '—', '-', 'EMPTY'):
                continue

            cargo = _clean_cargo_name(b.get('cargo'))
            is_mbc = _is_mbc_vessel(b, mbc_names_set=mbc_names_set)
            candidates.append({
                'id': b_id,
                'name': b.get('name', ''),
                'cargo': cargo,
                'berth': berth_raw,
                'balance': bal,
                'is_mbc': is_mbc
            })
            if b_id:
                seen_ids.add(b_id)
            seen_names.add(b_name_u)

    # 4. Grouping:
    #    - WR 19: If available at that time, shown separately with (WR 19)
    #    - Berth 10-12: Show ONLY MBC (not barges), shown separately with (Berth No.X)
    #    - Other berths (Berths 1-9 / Jetty): Show all barges or MBC cargo balance aggregated by cargo
    jetty_cargo_map = {}
    special_map = {}

    for c in candidates:
        b_raw = c['berth']
        bal = c['balance']
        cargo = c['cargo']
        is_mbc = c.get('is_mbc', False)

        # Check WR 19: if available at that time, show with (WR 19)
        if _is_wr19(b_raw):
            key = ('WR 19', cargo)
            if key not in special_map:
                special_map[key] = {
                    'berth': 'WR 19',
                    'cargo': cargo,
                    'balance': 0.0,
                    'note': '(WR 19)',
                    'sort_key': (2, 19, cargo)
                }
            special_map[key]['balance'] += bal
            continue

        # Check Berth 10 to 12: show ONLY MBC, not barges
        is_10_12, b_num = _is_berth_10_to_12(b_raw)
        if is_10_12:
            if not is_mbc:
                # 10, 11, and 12 berth show ONLY MBC, not barges
                continue
            key = (f"BERTH {b_num}", cargo)
            if key not in special_map:
                special_map[key] = {
                    'berth': f"BERTH {b_num}",
                    'cargo': cargo,
                    'balance': 0.0,
                    'note': f"(Berth No.{b_num})",
                    'sort_key': (1, int(b_num), cargo)
                }
            special_map[key]['balance'] += bal
            continue

        # Other berths (Berths 1-9 / Jetty): show all barges or MBC cargo balance aggregated by cargo
        if cargo not in jetty_cargo_map:
            jetty_cargo_map[cargo] = 0.0
        jetty_cargo_map[cargo] += bal

    table_items = []
    # 1. Jetty items
    for cargo in sorted(jetty_cargo_map.keys()):
        r_bal = int(round(jetty_cargo_map[cargo]))
        if r_bal > 0:
            table_items.append({
                'berth': 'Jetty',
                'cargo': cargo,
                'balance': r_bal,
                'note': '',
                'is_special': False
            })

    # 2. Special items: Berth 10-12 and WR 19
    sorted_specials = sorted(special_map.values(), key=lambda x: x['sort_key'])
    for sp in sorted_specials:
        r_bal = int(round(sp['balance']))
        if r_bal > 0:
            table_items.append({
                'berth': sp['berth'],
                'cargo': sp['cargo'],
                'balance': r_bal,
                'note': sp['note'],
                'is_special': True
            })

    grand_total = sum(it['balance'] for it in table_items)

    # 5. Build WhatsApp / SMS Text Block (EXACTLY matching format, without note)
    sms_lines = [f"Cargo Balance at Jetty for {shift_key} Shift", ""]
    if table_items:
        max_c_len = max(len(it['cargo']) for it in table_items)
        max_c_len = max(max_c_len, 14)
        for it in table_items:
            c_str = it['cargo'].ljust(max_c_len)
            b_str = f"{it['balance']:,} MT."
            if it['note']:
                sms_lines.append(f"{c_str} : {b_str} {it['note']}")
            else:
                sms_lines.append(f"{c_str} : {b_str}")

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
        'title': f"Cargo Balance at Jetty for {shift_key} Shift",
        'items': table_items,
        'total_qty': grand_total,
        'total_balance': grand_total,
        'sms_text': sms_text
    }


def _current_shift_code(now=None):
    now = now or datetime.now()
    hour = now.hour
    if 6 <= hour < 14:
        return 'A'
    if 14 <= hour < 22:
        return 'B'
    return 'C'


def _operational_date(now=None):
    now = now or datetime.now()
    d = now.date()
    if now.hour < 6:
        d -= timedelta(days=1)
    return d


# ── Routes ───────────────────────────────────────────────────────────────────

@bp.route('/module/RP01/shift-cargo-balance/')
@login_required
def shift_cargo_balance_index():
    now = datetime.now()
    cur_op = _operational_date(now)
    if cur_op < REPORT_CUTOFF_DATE:
        cur_op = REPORT_CUTOFF_DATE
    today_str = cur_op.strftime('%Y-%m-%d')
    cur_shift = _current_shift_code(now)
    return render_template(
        'shift_cargo_balance/shift_cargo_balance.html',
        username=session.get('username'),
        default_date=today_str,
        default_shift=cur_shift
    )


@bp.route('/api/module/RP01/shift-cargo-balance/preview')
@bp.route('/api/module/RP01/shift-cargo-balance/data')
@login_required
def shift_cargo_balance_data():
    now = datetime.now()
    entry_date = (request.args.get('entry_date') or request.args.get('date') or '').strip()
    shift = (request.args.get('shift') or '').strip().upper()

    if not entry_date:
        entry_date = _operational_date(now).strftime('%Y-%m-%d')
    if shift not in ('A', 'B', 'C'):
        shift = _current_shift_code(now)

    data = _fetch_shift_cargo_balance(entry_date, shift)
    return jsonify(data)


@bp.route('/api/module/RP01/shift-cargo-balance/download')
@login_required
def shift_cargo_balance_download():
    now = datetime.now()
    entry_date = (request.args.get('entry_date') or request.args.get('date') or '').strip()
    shift = (request.args.get('shift') or '').strip().upper()

    if not entry_date:
        entry_date = _operational_date(now).strftime('%Y-%m-%d')
    if shift not in ('A', 'B', 'C'):
        shift = _current_shift_code(now)

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

    # Row 1: Title (Cargo Balance at Jetty for <shift> Shift)
    ws.cell(1, 1, f"Cargo Balance at Jetty for {shift} Shift").font = font_title

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
        berth_display = it['berth']
        if it.get('note'):
            berth_display = f"{it['berth']} {it['note']}"
        c_berth = ws.cell(row_idx, 1, berth_display)
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

    # Set column widths
    ws.column_dimensions['A'].width = 22
    ws.column_dimensions['B'].width = 34
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
