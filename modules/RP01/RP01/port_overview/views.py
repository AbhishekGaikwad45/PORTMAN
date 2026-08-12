import json
import calendar
from datetime import date, datetime, timedelta
from functools import wraps

from flask import render_template, session, redirect, url_for, jsonify

from .. import bp
from database import get_db, get_cursor
from ..daily_ops.model import fy_label
from ..shift_report.views import _fetch_delays, _fetch_shift_pivot
from ..Barge_Position_Report.views import _fetch_tide_data, _fetch_all_barges
from weather_service import get_weather
from flask import render_template, session, redirect, url_for, jsonify, request

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


def _val(row, *fields):
    """First non-blank value among fields ('' if none). Dates here are text."""
    for f in fields:
        v = str(row.get(f) or '').strip()
        if v:
            return v
    return ''


# Anything logged at the port. Once one of these exists the vessel has arrived,
# so it is no longer upcoming — this is the "nothing after Gull Island" test.
_BARGE_AT_PORT = ('amf_at_port', 'along_side_berth', 'commence_discharge_berth',
                  'completed_discharge_berth', 'cast_off_berth', 'cast_off_port')
_MBC_AT_PORT = ('arrived_yellow_crane', 'vessel_arrival_port', 'vessel_all_made_fast',
                'unloading_commenced', 'cleaning_commenced', 'cleaning_completed',
                'unloading_completed', 'vessel_cast_off', 'sailed_out_load_port')


def _upcoming_arrivals():
    """Departed Gull Island, nothing logged at the port since — inbound now.

    Barge rule: loaded (loading done / cast off the MV) + an aweigh-from-Gull-
    Island time + nothing at the port yet. LDUD01 has two aweigh columns and
    the grids disagree on which carries the loaded leg — Import labels
    aweigh_gull_island_empty "Aweigh Gull Island (Loaded)", Export uses
    aweigh_gull_island — so accept either. Requiring "loaded" is what keeps the
    outbound empty leg (which also sets an aweigh) out of the list.
    Export LDUDs sail away from the port, never toward a berth, so they're out.
    """
    conn = get_db()
    cur = get_cursor(conn)

    # Latest line per barge by id: trips are entered as they happen, and the
    # newest trip is not necessarily on the newest LDUD document.
    cur.execute("""
        SELECT DISTINCT ON (UPPER(TRIM(l.barge_name)))
               TRIM(l.barge_name)                AS name,
               l.cargo_name,
               COALESCE(l.discharge_quantity, 0) AS qty,
               l.aweigh_gull_island_empty, l.aweigh_gull_island,
               l.completed_loading, l.cast_off_mv,
               l.amf_at_port, l.along_side_berth, l.commence_discharge_berth,
               l.completed_discharge_berth, l.cast_off_berth, l.cast_off_port
        FROM ldud_barge_lines l
        JOIN ldud_header h ON h.id = l.ldud_id
        WHERE COALESCE(TRIM(l.barge_name), '') <> ''
          AND COALESCE(h.operation_type, '') <> 'Export'
        ORDER BY UPPER(TRIM(l.barge_name)), l.id DESC
    """)
    upcoming = []
    for r in cur.fetchall():
        since = _val(r, 'aweigh_gull_island_empty', 'aweigh_gull_island')
        loaded = _val(r, 'completed_loading', 'cast_off_mv')
        if since and loaded and not _val(r, *_BARGE_AT_PORT):
            upcoming.append({'type': 'BARGE', 'name': r['name'], 'cargo': r['cargo_name'] or '',
                             'qty': float(r['qty'] or 0), 'since': _fmt_since(since)})

    cur.execute("""
        SELECT DISTINCT ON (UPPER(TRIM(h.mbc_name)))
               TRIM(h.mbc_name)           AS name,
               h.cargo_name,
               COALESCE(h.bl_quantity, 0) AS qty,
               d.departure_gull_island,
               d.arrived_yellow_crane, d.vessel_arrival_port, d.vessel_all_made_fast,
               d.unloading_commenced, d.cleaning_commenced, d.cleaning_completed,
               d.unloading_completed, d.vessel_cast_off, d.sailed_out_load_port
        FROM mbc_discharge_port_lines d
        JOIN mbc_header h ON h.id = d.mbc_id
        WHERE COALESCE(TRIM(h.mbc_name), '') <> ''
        ORDER BY UPPER(TRIM(h.mbc_name)), d.id DESC
    """)
    for r in cur.fetchall():
        since = _val(r, 'departure_gull_island')
        if since and not _val(r, *_MBC_AT_PORT):
            upcoming.append({'type': 'MBC', 'name': r['name'], 'cargo': r['cargo_name'] or '',
                             'qty': float(r['qty'] or 0), 'since': _fmt_since(since)})

    conn.close()
    upcoming.sort(key=lambda u: u['since'])
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

def _operational_date(now=None):
    """RP01's business day runs 6:00 AM -> 6:00 AM, not midnight -> midnight
    (shift A starts at 6). Before 6 AM, cards/delays should still reflect
    the previous calendar day, not an empty new one."""
    now = now or datetime.now()
    d = now.date()
    if now.hour < 6:
        d -= timedelta(days=1)
    return d


def _todays_notes():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT notes FROM barge_position_report
        WHERE report_date = %s AND shift = %s
    """, (_operational_date().strftime('%Y-%m-%d'), _current_shift_code()))
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

def _current_shift_incharge():
    """Shift in-charge for the current shift, from barge_position_report."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT shift_incharge FROM barge_position_report
        WHERE report_date = %s AND shift = %s
    """, (_operational_date().strftime('%Y-%m-%d'), _current_shift_code()))
    row = cur.fetchone()
    conn.close()
    if not row:
        return ''
    return (row['shift_incharge'] or '').strip()


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
# MBC Status — live voyage-stage status per MBC (JSW Infra / JSW Shipping
# owned only), independent of the berth-occupancy layout above. Sourced
# from mbc_master joined to its latest mbc_header + load/discharge lines.
# ---------------------------------------------------------------------------

def _fetch_mbc_status(report_date=None):

    conn = get_db()
    cur = get_cursor(conn)

    cur.execute("""
        SELECT

            TRIM(m.mbc_name) AS mbc_name,

            CASE

                /* Cargo only shown for loaded / loading MBCs — empty in
                   every other stage (waiting/on-the-way/discharging). */
                WHEN h.id IS NULL
                THEN NULL

                WHEN
                    NULLIF(TRIM(l.arrived_load_port), '') IS NOT NULL
                    AND NULLIF(TRIM(l.loading_commenced), '') IS NULL
                THEN NULL

                WHEN
                    NULLIF(TRIM(d.unloading_completed), '') IS NOT NULL
                    AND d.sailed_out_load_port IS NOT NULL
                THEN NULL

                WHEN
                    NULLIF(TRIM(d.unloading_completed), '') IS NOT NULL
                THEN NULL

                WHEN COALESCE(NULLIF(TRIM(h.cargo_name), ''), '') <> ''
                THEN TRIM(h.cargo_name)

                ELSE NULL

            END AS cargo_name,

            CASE

                /* Empty : Waiting at Jaigad */
                WHEN h.id IS NULL
                THEN 'EMPTY : WAITING AT JAIGAD'

                /* Empty : Waiting at Load Port */
                WHEN
                    NULLIF(TRIM(l.arrived_load_port), '') IS NOT NULL
                    AND NULLIF(TRIM(l.loading_commenced), '') IS NULL
                THEN
                    'EMPTY : WAITING AT LOAD PORT'

                /* Under Loading */
                WHEN
                    NULLIF(TRIM(l.loading_commenced), '') IS NOT NULL
                    AND NULLIF(TRIM(l.loading_completed), '') IS NULL
                THEN
                    'UNDER LOADING'

                /* Loaded : Waiting at Load Port */
                WHEN
                    NULLIF(TRIM(l.loading_completed), '') IS NOT NULL
                    AND NULLIF(TRIM(l.cast_off_load_port), '') IS NULL
                THEN
                    'LOADED : WAITING AT LOAD PORT'

                /* Loaded : On the way to Gull */
                WHEN
                    NULLIF(TRIM(l.cast_off_load_port), '') IS NOT NULL
                    AND NULLIF(TRIM(d.arrival_gull_island), '') IS NULL
                THEN
                    'LOADED : ON THE WAY TO GULL'

                /* Loaded : Waiting at Gull */
                WHEN
                    NULLIF(TRIM(d.arrival_gull_island), '') IS NOT NULL
                    AND NULLIF(TRIM(d.departure_gull_island), '') IS NULL
                THEN
                    'LOADED : WAITING AT GULL'

                /* Loaded : On the way to Dharamtar */
                WHEN
                    NULLIF(TRIM(d.departure_gull_island), '') IS NOT NULL
                    AND NULLIF(TRIM(d.vessel_arrival_port), '') IS NULL
                THEN
                    'LOADED : ON THE WAY TO DHARAMTAR'

                /* Loaded : Waiting at Dharamtar */
                WHEN
                    NULLIF(TRIM(d.vessel_arrival_port), '') IS NOT NULL
                    AND NULLIF(TRIM(d.unloading_commenced), '') IS NULL
                THEN
                    'LOADED : WAITING AT DHARAMTAR'

                /* Under Discharge */
                WHEN
                    NULLIF(TRIM(d.unloading_commenced), '') IS NOT NULL
                    AND NULLIF(TRIM(d.unloading_completed), '') IS NULL
                THEN
                    'UNDER DISCHARGE AT DHARAMTAR'

                /* Empty : Waiting at Jaigad (returned) — vessel has sailed
                   out from the discharge port AND has already reached the
                   load port again, so it's no longer "on the way". */
                WHEN
                    NULLIF(TRIM(d.unloading_completed), '') IS NOT NULL
                    AND d.sailed_out_load_port IS NOT NULL
                    AND d.reached_load_port IS NOT NULL
                THEN
                    'EMPTY : WAITING AT JAIGAD'

                /* Empty : On the way to Jaigad */
                WHEN
                    NULLIF(TRIM(d.unloading_completed), '') IS NOT NULL
                    AND d.sailed_out_load_port IS NOT NULL
                THEN
                    'EMPTY : ON THE WAY TO JAIGAD'

                /* Empty : Waiting at Dharamtar */
                WHEN
                    NULLIF(TRIM(d.unloading_completed), '') IS NOT NULL
                THEN
                    'EMPTY : WAITING AT DHARAMTAR'

                ELSE
                    'NA'

            END AS mbc_status

        FROM mbc_master m

        LEFT JOIN LATERAL (
            SELECT h.*
            FROM mbc_header h
            WHERE TRIM(h.mbc_name) = TRIM(m.mbc_name)
            ORDER BY h.id DESC
            LIMIT 1
        ) h ON TRUE

        LEFT JOIN mbc_load_port_lines l
            ON l.mbc_id = h.id

        LEFT JOIN mbc_discharge_port_lines d
            ON d.mbc_id = h.id

        WHERE
            UPPER(TRIM(COALESCE(m.mbc_owner_name, '')))
            IN ('JSW INFRA', 'JSW SHIPPING')

        ORDER BY m.mbc_name
    """)

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


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

def _shift_totals(date_s):
    """Total quantity per shift for a single date — reuses the shift-wise
    discharge pivot logic from the Shift Report (_fetch_shift_pivot), just
    collapsed across cargo since the card only needs a per-shift total."""
    pivot = _fetch_shift_pivot(date_s, 'ALL')
    totals = {}
    for cargo, shifts in pivot['data'].items():
        for shift, qty in shifts.items():
            totals[shift] = totals.get(shift, 0) + qty
    return {s: round(totals.get(s, 0), 2) for s in pivot['shifts'] if totals.get(s, 0)}


def _top_delays_today():
    """Today's delays (all shifts) for the current *operational* day
    (6 AM -> 6 AM), summed per (equipment/system, reason) and ranked."""
    today_s = _operational_date().strftime('%Y-%m-%d')
    delays = _fetch_delays(today_s, 'ALL')
    totals = {}
    for d in delays:
        name = (d.get('delay_name') or '(blank)').strip()
        if name.lower() in ('idle', 'unloading'):
            continue
        delay_type = (d.get('delay_type') or '').strip().lower().replace(' ', '')
        if delay_type == 'maintenancedelays':
            source = (d.get('system_name') or '').strip()
        else:
            source = (d.get('equipment_name') or '').strip()
        key = f"{source} — {name}" if source else name
        totals[key] = totals.get(key, 0) + int(d.get('total_minutes') or 0)
    ranked = [{'delay_name': k, 'minutes': v} for k, v in totals.items() if v > 0]
    ranked.sort(key=lambda x: x['minutes'], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _weather_card(now):
    """Cached WeatherAPI payload trimmed to the fields the weather card shows.

    None when the cache is empty or stale (see weather_service.TTL_HOURS) — the
    card then renders a 'no data' state instead of showing old weather.
    """
    w = get_weather()
    if not w:
        return None
    cur = w.get('current') or {}
    days = (w.get('forecast') or {}).get('forecastday') or []
    day0 = days[0] if days else {}
    now_s = now.strftime('%Y-%m-%d %H:%M')
    hours = [h for d in days for h in (d.get('hour') or []) if h.get('time', '') >= now_s][:6]
    return {
        'temp':     cur.get('temp_c'),
        'feels':    cur.get('feelslike_c'),
        'text':     (cur.get('condition') or {}).get('text'),
        'icon':     (cur.get('condition') or {}).get('icon'),
        'wind':     cur.get('wind_kph'),
        'wind_dir': cur.get('wind_dir'),
        'gust':     cur.get('gust_kph'),
        'humidity': cur.get('humidity'),
        'pressure': cur.get('pressure_mb'),
        'vis':      cur.get('vis_km'),
        'rain':     (day0.get('day') or {}).get('totalprecip_mm'),
        'sunrise':  (day0.get('astro') or {}).get('sunrise'),
        'sunset':   (day0.get('astro') or {}).get('sunset'),
        'updated':  cur.get('last_updated'),
        'hours':    [{'t': h['time'][11:16], 'temp': h.get('temp_c'),
                      'icon': (h.get('condition') or {}).get('icon'),
                      'rain': h.get('chance_of_rain')} for h in hours],
    }


@bp.route('/api/module/RP01/port-overview/data')
@login_required
def port_overview_data():
    now = datetime.now()
    today = _operational_date(now)          # was: date.today()
    today_s = today.strftime('%Y-%m-%d')
    yesterday_date = today - timedelta(days=1)
    yesterday_s = yesterday_date.strftime('%Y-%m-%d')
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

    target_base, target_effective = _fy_target_totals(current_fy_label)
    month_target = _month_target(current_fy_label, today.month)

    today_target_month     = _daily_target_by_days_left(today, 'month')
    today_target_fy        = _daily_target_by_days_left(today, 'fy')
    yesterday_target_month = _daily_target_by_days_left(yesterday_date, 'month')
    yesterday_target_fy    = _daily_target_by_days_left(yesterday_date, 'fy')

    cards = {
        'all_time':      {'label': 'All Time',              'by_type': all_time},
        'current_fy':    {
            'label': f'FY {current_fy_label}',
            'by_type': current_fy_by_type,
            'target': target_base,
            'target_effective': target_effective,
            'required_daily': today_target_fy,
        },
        'current_month': {
            'label': today.strftime('%b %Y'),
            'by_type': _cargo_by_type(month_start_s, today_s),
            'target': month_target,
            'required_daily': today_target_month,
        },
        'yesterday':     {
            'label': 'Yesterday',
            'by_type': _cargo_by_type(yesterday_s, yesterday_s),
            'by_shift': _shift_totals(yesterday_s),
            'target': yesterday_target_month,
            'target_fy': yesterday_target_fy,
        },
        'today':         {
            'label': 'Today',
            'by_type': _cargo_by_type(today_s, today_s),
            'by_shift': _shift_totals(today_s),
            'target': today_target_month,
            'target_fy': today_target_fy,
        },
    }
    for c in cards.values():
        c['total'] = round(sum(c['by_type'].values()), 2)
        c['by_type'] = {k: round(v, 2) for k, v in c['by_type'].items() if v > 0}
        if 'by_shift' in c:
            c['by_shift'] = {k: round(v, 2) for k, v in c['by_shift'].items() if v > 0}

    # MBC Status — same live data used everywhere else in RP01, fetched
    # fresh on every dashboard refresh so it never drifts from mbc_master.
    mbc_status_rows = _fetch_mbc_status(today_s)
    mbc_status = [
        {
            'mbc_name':    r['mbc_name'],
            'cargo_name':  r['cargo_name'],
            'mbc_status':  r['mbc_status'],
        }
        for r in mbc_status_rows
    ]

    return jsonify({
        'layout':      layout,
        'tide':        _fetch_tide_data(now, now),
        'notes':       _todays_notes(),
        'shift_incharge':  _current_shift_incharge(),
        'cargo_cards': cards,
        'upcoming':    _upcoming_arrivals(),
        'delays':      _top_delays_today(),
        'mbc_status':  mbc_status,
        'weather':     _weather_card(now),
        'as_of':       now.strftime('%Y-%m-%d %H:%M:%S'),
    })

# ---------------------------------------------------------------------------
# Targets (FY monthly ABP + Outlook, with category-wise breakdown)
# ---------------------------------------------------------------------------

TARGET_MONTHS = [
    ('April', 4), ('May', 5), ('June', 6), ('July', 7),
    ('August', 8), ('September', 9), ('October', 10), ('November', 11),
    ('December', 12), ('January', 1), ('February', 2), ('March', 3),
]

# Same categories the Port Overview cargo cards are keyed by (cargo_type),
# minus Finish Goods / Others which don't get monthly targets set.
TARGET_CATEGORIES = ['IBRM', 'CBRM', 'FLUXES', 'CLINKER', 'SLAG']


def _default_fy_targets():
    return [
        {
            'month': name,
            'month_num': num,
            'base_target': 0,
            'outlook': None,
            'categories': {cat: {'base': 0, 'outlook': None} for cat in TARGET_CATEGORIES},
        }
        for name, num in TARGET_MONTHS
    ]


def _fy_options(today=None):
    """Dropdown options: current FY, next FY, and 5 prior FYs."""
    today = today or date.today()
    current_fy_start = today.year if today.month >= 4 else today.year - 1
    options = []
    for start_year in range(current_fy_start + 1, current_fy_start - 5, -1):
        options.append(fy_label(start_year))
    return options


def _load_fy_targets(financial_year):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT targets FROM financial_year_targets WHERE financial_year = %s
    """, (financial_year,))
    row = cur.fetchone()
    conn.close()

    targets = row['targets'] if row else None
    if isinstance(targets, str):
        try:
            targets = json.loads(targets)
        except Exception:
            targets = None
    if not targets:
        return _default_fy_targets()

    # Merge with defaults so a FY saved before a month/category existed
    # never comes back with missing keys.
    by_month = {t.get('month_num'): t for t in targets if isinstance(t, dict)}
    merged = []
    for name, num in TARGET_MONTHS:
        saved = by_month.get(num) or {}
        cats = saved.get('categories') or {}
        merged.append({
            'month': name,
            'month_num': num,
            'base_target': saved.get('base_target') or 0,
            'outlook': saved.get('outlook'),
            'categories': {
                cat: {
                    'base': (cats.get(cat) or {}).get('base') or 0,
                    'outlook': (cats.get(cat) or {}).get('outlook'),
                }
                for cat in TARGET_CATEGORIES
            },
        })
    return merged


def _save_fy_targets(financial_year, targets):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        INSERT INTO financial_year_targets (financial_year, targets, updated_at)
        VALUES (%s, %s::jsonb, NOW())
        ON CONFLICT (financial_year)
        DO UPDATE SET targets = EXCLUDED.targets, updated_at = NOW()
    """, (financial_year, json.dumps(targets)))
    conn.commit()
    conn.close()

#Accept

def _fy_target_totals(financial_year):
    """Sum of monthly Base Target and Effective (Outlook-aware) values for a FY."""
    targets = _load_fy_targets(financial_year)
    base_total = sum(t.get('base_target') or 0 for t in targets)
    effective_total = sum(
        (t.get('outlook') if t.get('outlook') not in (None, '') else t.get('base_target')) or 0
        for t in targets
    )
    return round(base_total, 2), round(effective_total, 2)


def _month_target(financial_year, month_num):
    """Effective target (Outlook if set, else Base) for one month of a FY."""
    targets = _load_fy_targets(financial_year)
    row = next((t for t in targets if t.get('month_num') == month_num), None)
    if not row:
        return 0
    outlook = row.get('outlook')
    base = row.get('base_target') or 0
    value = outlook if outlook not in (None, '') else base
    return round(value or 0, 2)


def _achieved_between(start_s, end_s):
    """Total actual quantity across all cargo types, start..end inclusive."""
    by_type = _cargo_by_type(start_s, end_s)
    return round(sum(by_type.values()), 2)


def _days_left_in_month(d):
    """Days from d to month-end, inclusive of d."""
    days_in_month = calendar.monthrange(d.year, d.month)[1]
    return days_in_month - d.day + 1


def _days_left_in_fy(d):
    """Days from d to FY-end (Mar 31), inclusive of d."""
    fy_start_year = d.year if d.month >= 4 else d.year - 1
    fy_end = date(fy_start_year + 1, 3, 31)
    return (fy_end - d).days + 1


def _daily_target_by_days_left(d, scope='month'):
    """
    Daily target for date d = (period target so far unachieved) / (days left
    in the period, including d). scope is 'month' or 'fy'.
    """
    fy_start_year = d.year if d.month >= 4 else d.year - 1
    fy = fy_label(fy_start_year)

    if scope == 'month':
        total_target = _month_target(fy, d.month)
        period_start = d.replace(day=1)
        days_left = _days_left_in_month(d)
    else:  # 'fy'
        total_target = _fy_target_totals(fy)[1]
        period_start = date(fy_start_year, 4, 1)
        days_left = _days_left_in_fy(d)

    achieved_before_d = 0
    if d > period_start:
        achieved_before_d = _achieved_between(
            period_start.strftime('%Y-%m-%d'),
            (d - timedelta(days=1)).strftime('%Y-%m-%d')
        )

    remaining_target = total_target - achieved_before_d
    return round(remaining_target / days_left, 2) if days_left else 0

@bp.route('/module/RP01/port-overview/targets')
@login_required
def port_overview_targets():
    return render_template('port_overview/targets.html', username=session.get('username'))


@bp.route('/api/module/RP01/port-overview/targets', methods=['GET'])
@login_required
def port_overview_targets_get():
    today = date.today()
    default_fy = fy_label(today.year if today.month >= 4 else today.year - 1)
    financial_year = (request.args.get('fy') or default_fy).strip()

    return jsonify({
        'financial_year': financial_year,
        'fy_options': _fy_options(),
        'categories': TARGET_CATEGORIES,
        'targets': _load_fy_targets(financial_year),
        'totals': _fy_target_totals(financial_year),
    })


@bp.route('/api/module/RP01/port-overview/targets', methods=['POST'])
@login_required
def port_overview_targets_save():
    payload = request.get_json(force=True) or {}
    financial_year = (payload.get('financial_year') or '').strip()
    targets = payload.get('targets')

    if not financial_year or not isinstance(targets, list):
        return jsonify({'error': 'financial_year and targets[] are required'}), 400

    # Basic shape validation so a bad payload can't corrupt the JSONB blob.
    valid_months = {num for _, num in TARGET_MONTHS}
    for row in targets:
        if not isinstance(row, dict) or row.get('month_num') not in valid_months:
            return jsonify({'error': 'invalid targets payload'}), 400

    _save_fy_targets(financial_year, targets)
    return jsonify({'status': 'ok', 'financial_year': financial_year})