import json
from datetime import date, datetime, timedelta
from functools import wraps

from flask import render_template, session, redirect, url_for, jsonify

from .. import bp
from database import get_db, get_cursor
from ..daily_ops.model import fy_label
from ..shift_report.views import _fetch_delays
from ..Barge_Position_Report.views import _fetch_tide_data, _fetch_all_barges

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


# Bank position -> tier outboard of the jetty face (A/S hugs the berth,
# D/B is double-banked outboard of it, and so on).
_TIER = {'A/S': 0, 'D/B': 1, 'T/B': 2, 'F/B': 3, 'S/B': 4}


def _fetch_berth_occupancy():
    """Latest saved Barge Position berth layout, refreshed with live balances."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT berth_layout FROM barge_position_report
        ORDER BY report_date DESC, updated_at DESC
        LIMIT 1
    """)
    row = cur.fetchone()
    conn.close()
    layout = (row['berth_layout'] if row else []) or []
    if isinstance(layout, str):
        try:
            layout = json.loads(layout)
        except Exception:
            layout = []

    live_rows, _ = _fetch_all_barges()
    live = {(b.get('name') or '').strip().upper(): b for b in live_rows}

    occupancy = {}
    for item in layout:
        berth = (item.get('berth') or '').strip().upper()
        name = (item.get('name') or '').strip()
        if not berth or berth == 'WAITING' or not name:
            continue
        lv = live.get(name.upper())
        # A zero balance means discharge is done, NOT that the berth is free —
        # _fetch_all_barges() drops a vessel only at cast off, so trust that list.
        if lv is None and live:
            continue  # dropped from the live list = cast off / removed in LUEU01/LDUD
        commenced = (item.get('unloading_commenced') or item.get('commence_discharge_berth') or '').strip()
        if not commenced and lv:
            commenced = (lv.get('unloading_commenced') or lv.get('commence_discharge_berth') or '').strip()
        position = (item.get('position') or 'A/S').upper()
        balance = float((lv.get('balance_qty') if lv else None) or item.get('balance') or 0)
        occupancy.setdefault(berth, []).append({
            'name':        name,
            'type':        (item.get('type') or 'BARGE').upper(),
            'cargo':       (lv.get('cargo') if lv else None) or item.get('cargo') or '',
            'position':    position,
            'tier':        _TIER.get(position, 0),
            'total_qty':   float((lv.get('total_qty') if lv else None) or item.get('total') or item.get('qty') or 0),
            'balance_qty': balance,
            # Same vocabulary as the Barge Position berth cards, so the map
            # colours and the dashboard colours can't drift apart.
            'status':      ('Discharge Completed' if balance <= 0 else 'Under Discharge') if commenced else 'Waiting',
            'commenced':   commenced,
        })
    return occupancy


def _upcoming_arrivals():
    """Sailed from Gull Island loaded, nothing logged since — i.e. inbound now.

    Barges: LDUD01 barge lines, latest trip per barge. Import trips carry the
    loaded leg in *_empty (the grid labels aweigh_gull_island_empty as "Aweigh
    Gull Island (Loaded)"), so that is the column to read. Export trips sail
    away from the port, never toward a berth, so they are excluded.
    MBCs: MBC01 discharge port lines, latest line per MBC.
    """
    conn = get_db()
    cur = get_cursor(conn)

    # DISTINCT ON picks the latest trip first; the emptiness test is applied to
    # that row only, so an older matching trip can't resurrect a berthed barge.
    cur.execute("""
        SELECT * FROM (
            SELECT DISTINCT ON (UPPER(TRIM(l.barge_name)))
                   TRIM(l.barge_name)                AS name,
                   l.cargo_name,
                   COALESCE(l.discharge_quantity, 0) AS qty,
                   l.aweigh_gull_island_empty        AS since,
                   l.amf_at_port, l.along_side_berth, l.commence_discharge_berth,
                   l.completed_discharge_berth, l.cast_off_berth, l.cast_off_port
            FROM ldud_barge_lines l
            JOIN ldud_header h ON h.id = l.ldud_id
            WHERE COALESCE(TRIM(l.barge_name), '') <> ''
              AND COALESCE(h.operation_type, '') <> 'Export'
            ORDER BY UPPER(TRIM(l.barge_name)),
                     l.ldud_id DESC, l.trip_number DESC NULLS LAST, l.id DESC
        ) t
        WHERE COALESCE(TRIM(t.since::text), '') <> ''
          AND COALESCE(TRIM(t.amf_at_port::text), '') = ''
          AND COALESCE(TRIM(t.along_side_berth::text), '') = ''
          AND COALESCE(TRIM(t.commence_discharge_berth::text), '') = ''
          AND COALESCE(TRIM(t.completed_discharge_berth::text), '') = ''
          AND COALESCE(TRIM(t.cast_off_berth::text), '') = ''
          AND COALESCE(TRIM(t.cast_off_port::text), '') = ''
        ORDER BY t.since
    """)
    upcoming = [{'type': 'BARGE', 'name': r['name'], 'cargo': r['cargo_name'] or '',
                 'qty': float(r['qty'] or 0), 'since': _fmt_since(r['since'])}
                for r in cur.fetchall()]

    cur.execute("""
        SELECT * FROM (
            SELECT DISTINCT ON (UPPER(TRIM(h.mbc_name)))
                   TRIM(h.mbc_name)              AS name,
                   h.cargo_name,
                   COALESCE(h.bl_quantity, 0)    AS qty,
                   d.departure_gull_island       AS since,
                   d.arrived_yellow_crane, d.vessel_arrival_port, d.vessel_all_made_fast,
                   d.unloading_commenced, d.unloading_completed,
                   d.vessel_cast_off, d.sailed_out_load_port
            FROM mbc_discharge_port_lines d
            JOIN mbc_header h ON h.id = d.mbc_id
            WHERE COALESCE(TRIM(h.mbc_name), '') <> ''
            ORDER BY UPPER(TRIM(h.mbc_name)), d.mbc_id DESC, d.id DESC
        ) t
        WHERE COALESCE(TRIM(t.since::text), '') <> ''
          AND COALESCE(TRIM(t.arrived_yellow_crane::text), '') = ''
          AND COALESCE(TRIM(t.vessel_arrival_port::text), '') = ''
          AND COALESCE(TRIM(t.vessel_all_made_fast::text), '') = ''
          AND COALESCE(TRIM(t.unloading_commenced::text), '') = ''
          AND COALESCE(TRIM(t.unloading_completed::text), '') = ''
          AND COALESCE(TRIM(t.vessel_cast_off::text), '') = ''
          AND COALESCE(TRIM(t.sailed_out_load_port::text), '') = ''
        ORDER BY t.since
    """)
    upcoming += [{'type': 'MBC', 'name': r['name'], 'cargo': r['cargo_name'] or '',
                  'qty': float(r['qty'] or 0), 'since': _fmt_since(r['since'])}
                 for r in cur.fetchall()]

    conn.close()
    return upcoming


def _fmt_since(v):
    """'2026-08-04 09:15:00' -> '04/08 09:15'; leave anything odd as-is."""
    if not v:
        return ''
    s = str(v).strip().replace('T', ' ')
    for n, fmt in ((19, '%Y-%m-%d %H:%M:%S'), (16, '%Y-%m-%d %H:%M')):
        try:
            return datetime.strptime(s[:n], fmt).strftime('%d/%m %H:%M')
        except Exception:
            pass
    return s


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


# ---------------------------------------------------------------------------
# FY / All-Time cumulative logic — mirrors barge_discharge_report exactly.
# Fixed baseline + current-FY (historic April + live May-onward) totals.
# No _compute_fy_throughput call, no daily_ops_cutoff dependency, so prior
# years can never silently drift or get zeroed by a stale override.
# ---------------------------------------------------------------------------

# Cumulative throughput per cargo_type, FY2012-2013 (Half Year) through
# FY2025-2026 (i.e. everything up to and including March 31, 2026).
# Sourced from the FY summary table (Total row). Keyed case-insensitively
# (matched via .strip().upper()) to survive whatever casing cargo_type
# actually has in vessel_cargo.
BASELINE_TILL_MAR_2026 = {
    "IBRM": 116229319,
    "FLUXES": 24038328,
    "CBRM": 52438887,
    "CLINKER": 3453541,
    "SLAG": 1439086,
    "FINISH GOODS": 157549,
    "OTHER": 354512,
    "OTHERS": 354512,   # in case master data defaults to 'Others'
}


def _fy_window(today):
    """Same FY/historic/live windowing rules as barge_discharge_report."""
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    fy_start = f"{fy_start_year}-04-01"
    historic_cutoff = f"{fy_start_year}-04-30"
    live_start = f"{fy_start_year}-05-01"
    return fy_start, historic_cutoff, live_start


def _current_fy_by_type(today_s):
    """
    Current FY total per cargo_type = rp01_historical_lueu (Apr 1 -> Apr 30)
    + lueu_lines (May 1 -> today). No _compute_fy_throughput call, so no risk
    of it silently re-including rp01_historical_lueu data for PRIOR years.
    """
    today = datetime.strptime(today_s, "%Y-%m-%d").date()
    fy_start, historic_cutoff, live_start = _fy_window(today)

    conn = get_db()
    cur = get_cursor(conn)

    cur.execute("""
        SELECT COALESCE(vc.cargo_type, 'Others') AS cargo_type,
               SUM(COALESCE(h.quantity, 0)) AS historic_total
        FROM rp01_historical_lueu h
        LEFT JOIN vessel_cargo vc
            ON LOWER(TRIM(vc.cargo_name)) = LOWER(TRIM(h.cargo_name))
        WHERE h.quantity IS NOT NULL
          AND h.entry_date BETWEEN %(fy_start)s::date AND %(historic_cutoff)s::date
        GROUP BY vc.cargo_type
    """, {"fy_start": fy_start, "historic_cutoff": historic_cutoff})
    historic_totals = {r['cargo_type']: float(r['historic_total'] or 0) for r in cur.fetchall()}

    cur.execute("""
        SELECT COALESCE(vc.cargo_type, 'Others') AS cargo_type,
               SUM(COALESCE(l.quantity, 0)) AS live_total
        FROM lueu_lines l
        LEFT JOIN vessel_cargo vc
            ON LOWER(TRIM(vc.cargo_name)) = LOWER(TRIM(l.cargo_name))
        WHERE l.is_deleted IS NOT TRUE
          AND l.quantity IS NOT NULL
          AND l.entry_date::date BETWEEN %(live_start)s::date AND %(today)s::date
        GROUP BY vc.cargo_type
    """, {"live_start": live_start, "today": today_s})
    live_totals = {r['cargo_type']: float(r['live_total'] or 0) for r in cur.fetchall()}
    conn.close()

    fy_totals = dict(historic_totals)
    for ctype, qty in live_totals.items():
        fy_totals[ctype] = fy_totals.get(ctype, 0) + qty
    return fy_totals


def _cumulative_by_type(today_s):
    """
    All-time per cargo_type = hardcoded BASELINE_TILL_MAR_2026 (through
    Mar 31, 2026) + current FY total (historic-April + live-May-onward,
    computed above). No proportional splitting needed here (unlike
    barge_discharge_report) since port_overview cards are keyed by
    cargo_type only, not cargo_category.
    """
    current_fy = _current_fy_by_type(today_s)
    cumulative = dict(current_fy)  # start from current-FY totals

    seen_other = False
    for ctype, baseline in BASELINE_TILL_MAR_2026.items():
        # 'OTHER' and 'OTHERS' are the same bucket in the baseline dict —
        # only add it once to avoid double counting.
        if ctype in ("OTHER", "OTHERS"):
            if seen_other:
                continue
            seen_other = True

        match = next((k for k in cumulative if k.strip().upper() == ctype), None)
        if match:
            cumulative[match] += baseline
        else:
            cumulative[ctype.title()] = baseline

    return current_fy, cumulative


def _top_delays_today():
    """Today's delays (all shifts), summed per (equipment, reason) and ranked."""
    today_s = date.today().strftime('%Y-%m-%d')
    delays = _fetch_delays(today_s, 'ALL')
    totals = {}
    for d in delays:
        name = (d.get('delay_name') or '(blank)').strip()
        if name.lower() in ('idle', 'unloading'):
            continue
        equip = (d.get('equipment_name') or '').strip()
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

    # Baseline + current-FY cumulative logic, matching barge_discharge_report
    # exactly — no _compute_fy_throughput, no daily_ops_cutoff dependency.
    current_fy_by_type, all_time = _cumulative_by_type(today_s)

    cards = {
        'all_time':      {'label': 'All Time',              'by_type': all_time},
        'current_fy':    {'label': f'FY {current_fy_label}', 'by_type': current_fy_by_type},
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
        'upcoming':    _upcoming_arrivals(),
        'delays':      _top_delays_today(),
        'as_of':       now.strftime('%Y-%m-%d %H:%M:%S'),
    })