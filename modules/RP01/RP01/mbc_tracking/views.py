from flask import render_template, session, redirect, url_for, jsonify
from functools import wraps
from datetime import datetime, date

from .. import bp
from database import get_db, get_cursor


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ── Hardcoded SOP targets (hours) ────────────────────────────────────────────
# Milestone chain in cycle order. Each entry:
# (key, line ('lp'|'dp'), column, milestone label, status while this is the
#  last recorded milestone, target hours allowed until the next milestone —
#  None means no SOP target for that stage)
MILESTONES = [
    ('arrived_jaigad',    'lp', 'arrived_load_port',        'Arrived & Anchored at Jaigad',     'Waiting at Jaigad',                  1),
    ('loading_commenced', 'lp', 'loading_commenced',        'Loading commenced',                'Under loading at Jaigad anchorage',  6),
    ('loading_completed', 'lp', 'loading_completed',        'Loading completed',                'Loaded — awaiting castoff',          1),
    ('castoff_jaigad',    'lp', 'cast_off_load_port',       'Castoff from Jaigad',              'In transit to Gull',                 12),
    ('arrived_gull',      'dp', 'arrival_gull_island',      'Arrived at Gull',                  'Loaded waiting at Gull',             9),
    ('dept_gull',         'dp', 'departure_gull_island',    'Dept. from Gull',                  'In transit to Dharamtar',            4),
    ('arrived_dharamtar', 'dp', 'vessel_arrival_port',      'Arrived at Dharamtar',             'Waiting at Dharamtar',               4),
    ('disch_commenced',   'dp', 'unloading_commenced',      'Discharge Commenced',              'Under disch at Dharamtar',           6),
    ('partly_stop',       'dp', 'discharge_stop_shifting',  'Partly Disch Stop',                'Partly disch — waiting at DPPL',     None),
    ('partly_start',      'dp', 'discharge_start_shifting', 'Partly Disch Start',               'Under disch at Dharamtar',           None),
    ('disch_completed',   'dp', 'unloading_completed',      'Discharge Completed',              'Disch completed — awaiting castoff', 1),
    ('castoff_dharamtar', 'dp', 'vessel_cast_off',          'Castoff from Dharamtar',           'Awaiting sail out from Dharamtar',   None),
    ('sailed_out',        'dp', 'sailed_out_load_port',     'Sailed out from Dharamtar Jetty',  'In Transit to Jaigad Port',          16),
    ('reached_jaigad',    'dp', 'reached_load_port',        'Reached Jaigad',                   'Cycle Completed',                    None),
]

# Leg-wise SOP benchmark exactly as given (hours); total=True rows are the
# yellow subtotal rows in the SOP sheet, tat=True the blue TAT row.
LEGS = [
    {'leg': 'Jaigad Arrival - Jaigad Loading Commenced (Preberthing delay)',        'target': 1,  'total': False},
    {'leg': 'Loading Commence - Loading Completion (Loading time)',                 'target': 6,  'total': False},
    {'leg': 'Loading Completed - Cast Off from Jaigad (Waiting after loading)',     'target': 1,  'total': False},
    {'leg': 'Total time taken at Jaigad',                                           'target': 8,  'total': True},
    {'leg': 'Jaigad Departure to Gull Arrival (Loaded Transit time)',               'target': 12, 'total': False},
    {'leg': 'Gull Arrival - Gull Departure (Waiting at Gull)',                      'target': 9,  'total': False},
    {'leg': 'Gull Departure - Dharamtar Arrival',                                   'target': 4,  'total': False},
    {'leg': 'Jaigad Departure - Dharamtar Arrival (Jaigad to Dharamtar)',           'target': 25, 'total': True},
    {'leg': 'Dharamtar Arrival to Disch Commenced (Preberthing delay)',             'target': 4,  'total': False},
    {'leg': 'Disch Commenced to Disch Completed (Unloading Time)',                  'target': 6,  'total': False},
    {'leg': 'Disch Completed to Cast Off from Dharamtar (Waiting after Unloading)', 'target': 1,  'total': False},
    {'leg': 'Total time taken at Dharamtar',                                        'target': 11, 'total': True},
    {'leg': 'Dharamtar Departure to Jaigad Arrival',                                'target': 16, 'total': True},
    {'leg': 'TAT',                                                                  'target': 60, 'total': True, 'tat': True},
]

# (from_col, to_col) per leg for computing actuals — index-aligned with LEGS
LEG_COLS = [
    ('arrived_load_port',     'loading_commenced'),
    ('loading_commenced',     'loading_completed'),
    ('loading_completed',     'cast_off_load_port'),
    ('arrived_load_port',     'cast_off_load_port'),
    ('cast_off_load_port',    'arrival_gull_island'),
    ('arrival_gull_island',   'departure_gull_island'),
    ('departure_gull_island', 'vessel_arrival_port'),
    ('cast_off_load_port',    'vessel_arrival_port'),
    ('vessel_arrival_port',   'unloading_commenced'),
    ('unloading_commenced',   'unloading_completed'),
    ('unloading_completed',   'vessel_cast_off'),
    ('vessel_arrival_port',   'vessel_cast_off'),
    ('sailed_out_load_port',  'reached_load_port'),
    ('arrived_load_port',     'reached_load_port'),
]

def _parse_ts(v):
    """Parse the TEXT timestamps stored by MBC01 (datetime-local values)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = str(v).strip()
    if not s:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


@bp.route('/module/RP01/mbc-tracking/')
@login_required
def mbc_tracking_index():
    return render_template('mbc_tracking/mbc_tracking.html',
                           username=session.get('username'))


@bp.route('/api/module/RP01/mbc-tracking/data')
@login_required
def mbc_tracking_data():
    conn = get_db()
    cur = get_cursor(conn)

    # Latest Jaigad-load-port trip per fleet MBC, with its latest load-port
    # and discharge-port line (MBC01 keeps one line per trip; latest wins).
    # ponytail: ILIKE '%JAIGAD%' instead of exact match — survives spelling
    # variants of "JSW JAIGAD PORT" in the port master.
    cur.execute('''
        SELECT f.mbc_name,
               t.id AS mbc_id, t.doc_num, t.cargo_name, t.bl_quantity, t.doc_status,
               lp.arrived_load_port, lp.loading_commenced, lp.loading_completed,
               lp.cast_off_load_port,
               dp.arrival_gull_island, dp.departure_gull_island, dp.vessel_arrival_port,
               dp.unloading_commenced, dp.discharge_stop_shifting, dp.discharge_start_shifting,
               dp.unloading_completed, dp.vessel_cast_off, dp.sailed_out_load_port,
               dp.reached_load_port
        FROM mbc_master f
        LEFT JOIN LATERAL (
            SELECT id, doc_num, cargo_name, bl_quantity, doc_status
            FROM mbc_header
            WHERE mbc_name = f.mbc_name AND load_port ILIKE '%JAIGAD%'
            ORDER BY id DESC LIMIT 1
        ) t ON TRUE
        LEFT JOIN LATERAL (
            SELECT * FROM mbc_load_port_lines WHERE mbc_id = t.id ORDER BY id DESC LIMIT 1
        ) lp ON TRUE
        LEFT JOIN LATERAL (
            SELECT * FROM mbc_discharge_port_lines WHERE mbc_id = t.id ORDER BY id DESC LIMIT 1
        ) dp ON TRUE
        WHERE f.mbc_name ILIKE '%JSW%'
        ORDER BY f.mbc_name
    ''')
    rows = cur.fetchall()

    # All Jaigad trips (not just latest per vessel) for leg-wise actuals
    cur.execute('''
        SELECT h.id, h.mbc_name,
               lp.arrived_load_port, lp.loading_commenced, lp.loading_completed,
               lp.cast_off_load_port,
               dp.arrival_gull_island, dp.departure_gull_island, dp.vessel_arrival_port,
               dp.unloading_commenced, dp.unloading_completed, dp.vessel_cast_off,
               dp.sailed_out_load_port, dp.reached_load_port
        FROM mbc_header h
        LEFT JOIN LATERAL (
            SELECT * FROM mbc_load_port_lines WHERE mbc_id = h.id ORDER BY id DESC LIMIT 1
        ) lp ON TRUE
        LEFT JOIN LATERAL (
            SELECT * FROM mbc_discharge_port_lines WHERE mbc_id = h.id ORDER BY id DESC LIMIT 1
        ) dp ON TRUE
        WHERE h.load_port ILIKE '%JAIGAD%'
    ''')
    trips = [dict(t) for t in cur.fetchall()]
    conn.close()

    # reached_load_port is never entered by operators, so the return to Jaigad
    # is taken from the same vessel's NEXT trip arrival ("Reached Load Port"
    # := next "Arrived Load Port"); a real reached_load_port wins if present.
    by_vessel = {}
    for t in trips:
        by_vessel.setdefault(t['mbc_name'], []).append(t)
    for vt in by_vessel.values():
        vt.sort(key=lambda t: (_parse_ts(t['arrived_load_port']) or datetime.max, t['id']))
        for cur_t, nxt in zip(vt, vt[1:]):
            if cur_t.get('reached_load_port') is None:
                cur_t['reached_load_port'] = nxt.get('arrived_load_port')

    # Actual averages per leg over three windows: today, month-to-date,
    # financial-year-to-date (Apr–Mar). A leg is bucketed by its start time.
    now = datetime.now()
    fy_year = now.year if now.month >= 4 else now.year - 1
    windows = {
        'today': datetime(now.year, now.month, now.day),
        'mtd':   datetime(now.year, now.month, 1),
        'ytd':   datetime(fy_year, 4, 1),
    }
    sums   = {w: [0.0] * len(LEG_COLS) for w in windows}
    counts = {w: [0] * len(LEG_COLS) for w in windows}
    trip_counts = {w: 0 for w in windows}
    for t in trips:
        arr = _parse_ts(t.get('arrived_load_port'))
        for w, start in windows.items():
            if arr and arr >= start:
                trip_counts[w] += 1
        for i, (col_a, col_b) in enumerate(LEG_COLS):
            ta, tb = _parse_ts(t.get(col_a)), _parse_ts(t.get(col_b))
            if ta and tb:
                dh = (tb - ta).total_seconds() / 3600.0
                # ponytail: drop negative and >30-day diffs — bad data entry
                if 0 <= dh <= 720:
                    for w, start in windows.items():
                        if ta >= start:
                            sums[w][i] += dh
                            counts[w][i] += 1
    legs = [dict(l, **{w: round(sums[w][i] / counts[w][i], 2) if counts[w][i] else None
                       for w in windows})
            for i, l in enumerate(LEGS)]

    now = datetime.now()
    vessels = []
    for r in rows:
        milestones = {}          # key -> display string
        last_idx = None
        last_ts = None
        for i, (key, _line, col, _label, _status, _target) in enumerate(MILESTONES):
            ts = _parse_ts(r.get(col))
            milestones[key] = ts.strftime('%d-%b-%y %H:%M') if ts else ''
            if ts is not None:
                last_idx, last_ts = i, ts

        if r.get('mbc_id') is None or last_idx is None:
            vessels.append({
                'mbc_name': r['mbc_name'], 'doc_num': r.get('doc_num') or '',
                'cargo': '', 'qty': None, 'status': '—',
                'last_milestone': 'Not yet arrived', 'milestone_date': '',
                'hrs_in_stage': None, 'target': None, 'variance': None,
                'rag': '', 'stage_idx': len(MILESTONES) + 1, 'active': False,
                'milestones': milestones,
            })
            continue

        key, _line, _col, label, status, target = MILESTONES[last_idx]
        hrs = round((now - last_ts).total_seconds() / 3600.0, 1)
        # ponytail: reached_load_port is almost never filled by operators, so a
        # sail-out older than 2x the return target (16h) counts as a finished
        # cycle instead of "in transit" forever; drops out when data improves.
        completed = key == 'reached_jaigad' or (key == 'sailed_out' and hrs > 32)
        if completed and key == 'sailed_out':
            status = 'Cycle Completed'

        if completed:
            rag, variance, hrs_disp, target_disp = '', None, None, None
        else:
            variance = round(hrs - target, 1) if target is not None else None
            if target is None:
                rag = ''
            elif hrs > target:
                rag = 'Delayed'
            elif target - hrs <= 1:
                # ponytail: Watch = within 1 hour of breaching target
                rag = 'Watch'
            else:
                rag = 'On Track'
            hrs_disp, target_disp = hrs, target

        vessels.append({
            'mbc_name': r['mbc_name'], 'doc_num': r.get('doc_num') or '',
            'cargo': r.get('cargo_name') or '',
            'qty': float(r['bl_quantity']) if r.get('bl_quantity') is not None else None,
            'status': status, 'last_milestone': label,
            'milestone_date': last_ts.strftime('%d-%b-%y %H:%M'),
            'hrs_in_stage': hrs_disp, 'target': target_disp,
            'variance': variance, 'rag': rag,
            'stage_idx': last_idx, 'active': not completed,
            'milestones': milestones,
        })

    # Mock ordering: active vessels by cycle progress, then completed/idle
    vessels.sort(key=lambda v: (0 if v['active'] else 1, v['stage_idx'], v['mbc_name']))

    active = [v for v in vessels if v['active']]
    breakdown = {}
    for v in active:
        breakdown[v['status']] = breakdown.get(v['status'], 0) + 1

    hrs_vals = [v['hrs_in_stage'] for v in active if v['hrs_in_stage'] is not None]
    kpis = {
        'fleet':      len(vessels),
        'active':     len(active),
        'on_track':   sum(1 for v in active if v['rag'] == 'On Track'),
        'watch':      sum(1 for v in active if v['rag'] == 'Watch'),
        'delayed':    sum(1 for v in active if v['rag'] == 'Delayed'),
        'avg_hrs':    round(sum(hrs_vals) / len(hrs_vals), 1) if hrs_vals else 0,
    }

    return jsonify({
        'as_of': now.strftime('%Y-%m-%d %H:%M:%S'),
        'kpis': kpis,
        'vessels': vessels,
        'status_breakdown': [{'status': s, 'count': c} for s, c in breakdown.items()],
        'legs': legs,
        'trip_counts': trip_counts,
        'today_label': now.strftime('%d-%m-%Y'),
        'fy_label': f'FY {fy_year % 100}-{(fy_year + 1) % 100}',
        'milestone_labels': [
            {'key': k, 'label': label} for (k, _l, _c, label, _s, _t) in MILESTONES
        ],
    })
