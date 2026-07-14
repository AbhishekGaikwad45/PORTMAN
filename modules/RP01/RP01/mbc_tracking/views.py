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


# ── Hardcoded SOP targets (days) ─────────────────────────────────────────────
# Milestone chain in cycle order. Each entry:
# (key, line ('lp'|'dp'), column, milestone label, status while this is the
#  last recorded milestone, target days allowed until the next milestone —
#  None means no SOP target for that stage)
MILESTONES = [
    ('arrived_jaigad',    'lp', 'arrived_load_port',        'Arrived & Anchored at Jaigad',     'Waiting at Jaigad',                  1),
    ('loading_commenced', 'lp', 'loading_commenced',        'Loading commenced',                'Under loading at Jaigad anchorage',  6),
    ('loading_completed', 'lp', 'loading_completed',        'Loading completed',                'Loaded — awaiting castoff',          0),
    ('castoff_jaigad',    'lp', 'cast_off_load_port',       'Castoff from Jaigad',              'In transit to Gull',                 12),
    ('arrived_gull',      'dp', 'arrival_gull_island',      'Arrived at Gull',                  'Loaded waiting at Gull',             10),
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

# Leg-wise SOP benchmark exactly as given (days); total=True rows are the
# yellow subtotal rows in the SOP sheet.
LEGS = [
    {'leg': 'Jaigad Arrival - Loading commence',        'target': 1,  'total': False},
    {'leg': 'Loading Commence - Loading Completion',    'target': 6,  'total': False},
    {'leg': 'Loading Complete to Cast off',             'target': 0,  'total': False},
    {'leg': 'Total Time Taken at Jaigad',               'target': 7,  'total': True},
    {'leg': 'Jaigad Departure to Gull Arrival',         'target': 12, 'total': False},
    {'leg': 'Gull arrival - Gull Departure',            'target': 10, 'total': False},
    {'leg': 'Gull Departure - Dharamtar Arrival',       'target': 4,  'total': False},
    {'leg': 'Jaigad Departure - Dharamtar Arrival',     'target': 26, 'total': True},
    {'leg': 'Dharamtar Arrival to Disch Commence',      'target': 4,  'total': False},
    {'leg': 'Disch Commence to Disch Completed',        'target': 6,  'total': False},
    {'leg': 'Disch Completed to Cast off',              'target': 1,  'total': False},
    {'leg': 'Total Time Taken at Dharamtar',            'target': 11, 'total': True},
    {'leg': 'Dharamtar Departure  to Jaigad Arrival',   'target': 16, 'total': True},
    {'leg': 'Total TAT',                                'target': 60, 'total': True},
]

ONE_WAY_TARGET = 44   # Jaigad (7) + transit (26) + Dharamtar (11)
TOTAL_TAT_TARGET = 60


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
    conn.close()

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
                'days_in_stage': None, 'target': None, 'variance': None,
                'rag': '', 'stage_idx': len(MILESTONES) + 1, 'active': False,
                'milestones': milestones,
            })
            continue

        key, _line, _col, label, status, target = MILESTONES[last_idx]
        days = round((now - last_ts).total_seconds() / 86400.0, 1)
        # ponytail: reached_load_port is almost never filled by operators, so a
        # sail-out older than 2x the return target (16d) counts as a finished
        # cycle instead of "in transit" forever; drops out when data improves.
        completed = key == 'reached_jaigad' or (key == 'sailed_out' and days > 32)
        if completed and key == 'sailed_out':
            status = 'Cycle Completed'

        if completed:
            rag, variance, days_disp, target_disp = '', None, None, None
        else:
            variance = round(days - target, 1) if target is not None else None
            if target is None:
                rag = ''
            elif days > target:
                rag = 'Delayed'
            elif target - days <= 1:
                # ponytail: Watch = within 1 day of breaching target
                rag = 'Watch'
            else:
                rag = 'On Track'
            days_disp, target_disp = days, target

        vessels.append({
            'mbc_name': r['mbc_name'], 'doc_num': r.get('doc_num') or '',
            'cargo': r.get('cargo_name') or '',
            'qty': float(r['bl_quantity']) if r.get('bl_quantity') is not None else None,
            'status': status, 'last_milestone': label,
            'milestone_date': last_ts.strftime('%d-%b-%y %H:%M'),
            'days_in_stage': days_disp, 'target': target_disp,
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

    days_vals = [v['days_in_stage'] for v in active if v['days_in_stage'] is not None]
    kpis = {
        'fleet':      len(vessels),
        'active':     len(active),
        'on_track':   sum(1 for v in active if v['rag'] == 'On Track'),
        'watch':      sum(1 for v in active if v['rag'] == 'Watch'),
        'delayed':    sum(1 for v in active if v['rag'] == 'Delayed'),
        'avg_days':   round(sum(days_vals) / len(days_vals), 1) if days_vals else 0,
    }

    return jsonify({
        'as_of': now.strftime('%Y-%m-%d %H:%M:%S'),
        'kpis': kpis,
        'vessels': vessels,
        'status_breakdown': [{'status': s, 'count': c} for s, c in breakdown.items()],
        'legs': LEGS,
        'milestone_targets': [
            {'milestone': label, 'target': target}
            for (_k, _l, _c, label, _s, target) in MILESTONES if target is not None
        ],
        'milestone_labels': [
            {'key': k, 'label': label} for (k, _l, _c, label, _s, _t) in MILESTONES
        ],
        'one_way_target': ONE_WAY_TARGET,
        'total_tat_target': TOTAL_TAT_TARGET,
    })
