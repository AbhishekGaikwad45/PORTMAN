import json
from datetime import date, datetime, timedelta
from functools import wraps

from flask import render_template, session, redirect, url_for, jsonify

from .. import bp
from database import get_db, get_cursor
from ..daily_ops.views import _compute_fy_throughput
from ..daily_ops.model import fy_label
from ..shift_report.views import _fetch_delays
from ..Barge_Position_Report.views import _fetch_tide_data

IMAGE_SIZE = (941, 1672)  # static/img/Clean_berths.png natural pixel size


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@bp.route('/module/RP01/port-overview/')
@login_required
def port_overview_index():
    return render_template('port_overview/port_overview.html', username=session.get('username'))


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_berth_layout():
    """Berth positions live on port_berth_master.image_position (PBM01)."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT berth_name, image_position
        FROM port_berth_master
        WHERE image_position IS NOT NULL
        ORDER BY berth_sequence NULLS LAST, berth_name
    """)
    rows = cur.fetchall()
    conn.close()

    berths = []
    for r in rows:
        pos = r['image_position']
        if isinstance(pos, str):
            try:
                pos = json.loads(pos)
            except Exception:
                pos = None
        if not pos:
            continue
        berths.append({
            'label': r['berth_name'],
            'cx':    pos.get('cx'),
            'cy':    pos.get('cy'),
            'w':     pos.get('w'),
            'h':     pos.get('h'),
            'angle': pos.get('angle', 0),
        })
    return {'image': 'Clean_berths.png', 'image_size': list(IMAGE_SIZE), 'berths': berths}


def _fetch_berth_occupancy():
    """Today's active barges/MBCs per berth, from LUEU (today's entries)."""
    today_s = date.today().strftime('%Y-%m-%d')
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT berth_name, source_type,
               COALESCE(barge_name, '') AS barge_name,
               MAX(source_display) AS source_display,
               COALESCE(MAX(cargo_name), '') AS cargo_name
        FROM lueu_lines
        WHERE entry_date = %s AND is_deleted IS NOT TRUE
          AND berth_name IS NOT NULL AND berth_name != ''
          AND (
              source_type = 'MBC'
              OR (source_type = 'VCN' AND barge_name IS NOT NULL AND barge_name != '')
          )
        GROUP BY berth_name, source_type, barge_name
        ORDER BY berth_name
    """, (today_s,))
    rows = cur.fetchall()
    conn.close()

    occupancy = {}
    for r in rows:
        berth = (r['berth_name'] or '').strip().upper()
        name = (r['barge_name'] or r['source_display'] or '').strip()
        if not berth or not name:
            continue
        occupancy.setdefault(berth, []).append({
            'type':  r['source_type'],
            'name':  name,
            'cargo': r['cargo_name'] or '',
        })
    return occupancy


def _current_shift_code(now=None):
    hour = (now or datetime.now()).hour
    if 6 <= hour < 14:
        return 'A'
    if 14 <= hour < 22:
        return 'B'
    return 'C'


def _todays_notes():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT notes FROM barge_position_report
        WHERE report_date = %s AND shift = %s
    """, (date.today().strftime('%Y-%m-%d'), _current_shift_code()))
    row = cur.fetchone()
    conn.close()
    if not row:
        return []
    notes = row['notes']
    if isinstance(notes, str):
        try:
            notes = json.loads(notes)
        except Exception:
            notes = []
    return notes or []


def _get_cutoff_editable_values():
    """Admin-set historical FY overrides (see daily_ops cutoff)."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT cutoff_values FROM daily_ops_cutoff ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    if not row:
        return {}
    values = row['cutoff_values']
    if isinstance(values, str):
        values = json.loads(values)
    return (values or {}).get('fy_throughput', {})


def _cargo_by_type(start_date_s, end_date_s):
    """Live cargo-type breakdown from LUEU for a date range (inclusive)."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT COALESCE(vc.cargo_type, 'OTHERS') AS cargo_type,
               COALESCE(SUM(l.quantity), 0) AS qty
        FROM lueu_lines l
        LEFT JOIN vessel_cargo vc
            ON UPPER(TRIM(vc.cargo_name)) = UPPER(TRIM(l.cargo_name))
        WHERE l.is_deleted IS NOT TRUE
          AND l.cargo_name IS NOT NULL
          AND NULLIF(BTRIM(l.entry_date), '') IS NOT NULL
          AND TO_DATE(BTRIM(l.entry_date), 'YYYY-MM-DD') BETWEEN %s AND %s
        GROUP BY COALESCE(vc.cargo_type, 'OTHERS')
        HAVING COALESCE(SUM(l.quantity), 0) > 0
        ORDER BY qty DESC
    """, (start_date_s, end_date_s))
    rows = cur.fetchall()
    conn.close()
    return {r['cargo_type']: float(r['qty']) for r in rows}


def _top_delays_today():
    """Today's delays (all shifts), summed per (equipment, reason) and ranked."""
    today_s = date.today().strftime('%Y-%m-%d')
    delays = _fetch_delays(today_s, 'ALL')
    totals = {}
    for d in delays:
        equip = (d.get('equipment_name') or '').strip()
        name = d.get('delay_name') or '(blank)'
        key = f"{equip} — {name}" if equip else name
        totals[key] = totals.get(key, 0) + int(d.get('total_minutes') or 0)
    ranked = [{'delay_name': k, 'minutes': v} for k, v in totals.items() if v > 0]
    ranked.sort(key=lambda x: x['minutes'], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@bp.route('/api/module/RP01/port-overview/data')
@login_required
def port_overview_data():
    now = datetime.now()
    today = date.today()
    today_s = today.strftime('%Y-%m-%d')
    yesterday_s = (today - timedelta(days=1)).strftime('%Y-%m-%d')
    month_start_s = today.replace(day=1).strftime('%Y-%m-%d')

    layout = _load_berth_layout()
    occupancy = _fetch_berth_occupancy()
    for b in layout.get('berths', []):
        b['assets'] = occupancy.get(str(b.get('label', '')).strip().upper(), [])

    current_fy_start = today.year if today.month >= 4 else today.year - 1
    current_fy_label = fy_label(current_fy_start)
    editable_fy = _get_cutoff_editable_values()
    # ponytail: a saved cutoff override must never zero the running FY here —
    # live lueu_lines + rp01_historical_lueu are the truth for the current year
    editable_fy.pop(current_fy_label, None)
    fy_data = _compute_fy_throughput(today_s, editable_fy)

    all_time = {}
    for fy_dict in fy_data.values():
        for ctype, qty in fy_dict.items():
            all_time[ctype] = all_time.get(ctype, 0) + qty

    cards = {
        'all_time':      {'label': 'All Time',              'by_type': all_time},
        'current_fy':    {'label': f'FY {current_fy_label}', 'by_type': fy_data.get(current_fy_label, {})},
        'current_month': {'label': today.strftime('%b %Y'),  'by_type': _cargo_by_type(month_start_s, today_s)},
        'yesterday':     {'label': 'Yesterday',              'by_type': _cargo_by_type(yesterday_s, yesterday_s)},
        'today':         {'label': 'Today',                  'by_type': _cargo_by_type(today_s, today_s)},
    }
    for c in cards.values():
        c['total'] = round(sum(c['by_type'].values()), 2)
        c['by_type'] = {k: round(v, 2) for k, v in c['by_type'].items() if v > 0}

    return jsonify({
        'layout':      layout,
        'tide':        _fetch_tide_data(now, now),
        'notes':       _todays_notes(),
        'cargo_cards': cards,
        'delays':      _top_delays_today(),
        'as_of':       now.strftime('%Y-%m-%d %H:%M:%S'),
    })
