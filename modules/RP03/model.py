"""RP03 — Arrival Planner: when does each barge / MBC reach Dharamtar.

Nothing in the schema stores an ETA. mbc_load_port_lines.eta exists but the
MBC01 form never writes it (0 of 253 rows filled), so an arrival time has to be
derived: take the latest milestone a trip has stamped and add the median
observed time from that milestone to arrival, measured over this port's own
completed trips.

A median is the whole model on purpose — a gradient-boosted regressor was tried
seven ways on this data and lost to it every time. The residual is driven by
tide windows, berth-priority calls and breakdowns that no column here records,
so the calibrated table is both more accurate and auditable. p10/p90 ride along
so the board can show a band rather than a false point estimate.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median, quantiles

from database import get_db, get_cursor

# Milestone chains, earliest first. A trip is placed at the LAST one it stamped.
BARGE_STAGES = [
    ('trip_start',           'Trip start'),
    ('along_side_vessel',    'Alongside mother vessel'),
    ('commenced_loading',    'Loading commenced'),
    ('completed_loading',    'Loading completed'),
    ('cast_off_mv',          'Cast off mother vessel'),
    ('anchored_gull_island', 'Anchored Gull Island'),
    ('aweigh_gull_island',   'Aweigh Gull Island'),
    ('amf_at_port',          'All made fast at port'),
]
# sailed_out_load_port is excluded on purpose: it is stamped AFTER arrival in
# 914 of 915 rows — the field is populated wrong upstream, so it would drag
# every MBC's ETA into the past.
MBC_STAGES = [
    ('arrived_load_port',      'Arrived load port'),
    ('lp_alongside_berth',     'Alongside load berth'),
    ('lp_loading_commenced',   'Loading commenced'),
    ('lp_loading_completed',   'Loading completed'),
    ('cast_off_load_port',     'Cast off load port'),
    ('arrival_gull_island',    'Arrived Gull Island'),
    ('departure_gull_island',  'Departed Gull Island'),
]

BARGE_ARRIVAL, BARGE_DONE = 'along_side_berth', 'completed_discharge_berth'
MBC_ARRIVAL,   MBC_DONE   = 'vessel_arrival_port', 'unloading_completed'

MIN_N = 30          # below this a stage has no trustworthy median
MAX_H = 240.0       # a leg longer than 10 days is a data error, not a voyage
STALE_DAYS = 7      # a trip untouched this long is an abandoned document
OPS_HOUR = 7        # the operational day runs 07:00 → 07:00
HORIZON_DAYS = 2    # today + tomorrow


def _dt(val):
    """Parse the mixed TEXT/TIMESTAMP datetimes these tables hold."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).strip())
    except Exception:
        return None


def _fetch():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT b.id, b.barge_name, b.trip_number, b.cargo_name, b.discharge_quantity,
               b.contractor_name, h.doc_num AS doc_num, h.vessel_name,
               b.trip_start, b.along_side_vessel, b.commenced_loading,
               b.completed_loading, b.cast_off_mv, b.anchored_gull_island,
               b.aweigh_gull_island, b.amf_at_port,
               b.along_side_berth, b.commence_discharge_berth, b.completed_discharge_berth
        FROM ldud_barge_lines b
        JOIN ldud_header h ON h.id = b.ldud_id
        WHERE COALESCE(b.barge_name, '') <> ''
    ''')
    barges = cur.fetchall()
    cur.execute('''
        SELECT m.id, m.mbc_name, m.doc_num, m.cargo_name, m.bl_quantity,
               m.quantity_uom, m.load_port, m.operation_type,
               lp.arrived_load_port,
               lp.alongside_berth      AS lp_alongside_berth,
               lp.loading_commenced    AS lp_loading_commenced,
               lp.loading_completed    AS lp_loading_completed,
               lp.cast_off_load_port,
               dp.arrival_gull_island, dp.departure_gull_island,
               dp.vessel_arrival_port, dp.unloading_commenced, dp.unloading_completed,
               dp.vessel_unloading_berth
        FROM mbc_header m
        LEFT JOIN LATERAL (SELECT * FROM mbc_load_port_lines      x WHERE x.mbc_id = m.id ORDER BY x.id LIMIT 1) lp ON TRUE
        LEFT JOIN LATERAL (SELECT * FROM mbc_discharge_port_lines x WHERE x.mbc_id = m.id ORDER BY x.id LIMIT 1) dp ON TRUE
        WHERE COALESCE(m.mbc_name, '') <> ''
    ''')
    mbcs = cur.fetchall()
    cur.close()
    conn.close()
    return barges, mbcs


def _leg_table(rows, stages, arrival_col):
    """{stage: (p10, p50, p90, n)} hours from that milestone to arrival."""
    samples = defaultdict(list)
    for r in rows:
        arrival = _dt(r[arrival_col])
        if not arrival:
            continue
        for col, _label in stages:
            t = _dt(r.get(col))
            if not t:
                continue
            hrs = (arrival - t).total_seconds() / 3600
            if 0 <= hrs <= MAX_H:
                samples[col].append(hrs)
    table = {}
    for col, vals in samples.items():
        if len(vals) < MIN_N:
            continue
        deciles = quantiles(vals, n=10)
        table[col] = (round(deciles[0], 1), round(median(vals), 1),
                      round(deciles[8], 1), len(vals))
    return table


def _open_trips(rows, stages, table, arrival_col, done_col, spec, now):
    """Live trips, each tagged 'inbound' / 'arrived' / 'unknown'.

    One flat list rather than three, so the caller can pick the latest trip per
    vessel ACROSS buckets — otherwise a barge on a fresh run shows up again as
    an older, never-discharged trip sitting in 'at port'."""
    labels = dict(stages)
    trips = []
    for r in rows:
        arrival = _dt(r[arrival_col])
        done = _dt(r[done_col])
        last = None
        for col, _label in stages:
            t = _dt(r.get(col))
            if t:
                last = (col, t)
        seen_at = arrival or (last[1] if last else None)
        if not seen_at or seen_at < now - timedelta(days=STALE_DAYS):
            continue                        # abandoned document, not a voyage
        trip = {
            'kind':       spec['kind'],
            'name':       (r[spec['name']] or '').strip(),
            'doc':        r.get('doc_num') or '',
            'cargo':      (r.get('cargo_name') or '').strip(),
            'qty':        r.get(spec['qty']),
            'uom':        (r.get('quantity_uom') or 'MT'),
            'via':        (r.get(spec['via']) or '').strip(),
            'trip_label': spec['trip_label'](r),
            'last_stage': labels.get(last[0], '') if last else '',
            'last_at':    last[1] if last else None,
            'sort_at':    seen_at,
        }
        if arrival:
            if done:
                continue                    # discharged and gone
            trip.update({'bucket': 'arrived', 'arrived_at': arrival})
            trips.append(trip)
            continue
        band = table.get(last[0]) if last else None
        if not band:
            # Either no milestone at all, or one with too few past samples to
            # average — show it, don't guess. Hiding it loses the cargo.
            trip['bucket'] = 'unknown'
            trips.append(trip)
            continue
        p10, p50, p90, n = band
        trip.update({
            'bucket':  'inbound',
            'eta':     last[1] + timedelta(hours=p50),
            'eta_p10': last[1] + timedelta(hours=p10),
            'eta_p90': last[1] + timedelta(hours=p90),
            'leg_p50': p50,
            'leg_n':   n,
        })
        trips.append(trip)
    return trips


def _latest_per_vessel(trips):
    """One row per barge / MBC — the trip with the most recent milestone."""
    best = {}
    for t in trips:
        key = (t['kind'], t['name'].upper())
        if key not in best or t['sort_at'] > best[key]['sort_at']:
            best[key] = t
    return list(best.values())


def board(now=None):
    """Two operational days of hourly arrival slots, plus what falls outside."""
    now = now or datetime.now()
    barges, mbcs = _fetch()

    barge_table = _leg_table(barges, BARGE_STAGES, BARGE_ARRIVAL)
    mbc_table   = _leg_table(mbcs,   MBC_STAGES,   MBC_ARRIVAL)

    trips = _latest_per_vessel(
        _open_trips(barges, BARGE_STAGES, barge_table, BARGE_ARRIVAL, BARGE_DONE,
                    {'kind': 'BARGE', 'name': 'barge_name', 'qty': 'discharge_quantity',
                     'via': 'vessel_name',
                     'trip_label': lambda r: 'Trip %s' % r['trip_number'] if r.get('trip_number') else ''},
                    now)
        + _open_trips(mbcs, MBC_STAGES, mbc_table, MBC_ARRIVAL, MBC_DONE,
                      {'kind': 'MBC', 'name': 'mbc_name', 'qty': 'bl_quantity',
                       'via': 'load_port', 'trip_label': lambda r: r.get('doc_num') or ''},
                      now))
    inbound = [t for t in trips if t['bucket'] == 'inbound']
    at_port = [t for t in trips if t['bucket'] == 'arrived']
    unknown = [t for t in trips if t['bucket'] == 'unknown']

    # Operational day: before 07:00 we are still inside yesterday's ops day.
    ops_start = now.replace(hour=OPS_HOUR, minute=0, second=0, microsecond=0)
    if now.hour < OPS_HOUR:
        ops_start -= timedelta(days=1)

    slots = [[[] for _ in range(HORIZON_DAYS)] for _ in range(24)]
    overdue, beyond = [], []
    for t in sorted(inbound, key=lambda x: x['eta']):
        offset = (t['eta'] - ops_start).total_seconds() / 3600
        if offset < 0:
            overdue.append(t)               # ETA has passed, arrival not stamped
        elif offset >= 24 * HORIZON_DAYS:
            beyond.append(t)
        else:
            slots[int(offset) % 24][int(offset) // 24].append(t)

    days = [{'start': ops_start + timedelta(days=d),
             'end':   ops_start + timedelta(days=d + 1),
             'label': ('Today', 'Tomorrow')[d] if d < 2 else 'Day %d' % (d + 1)}
            for d in range(HORIZON_DAYS)]
    live_hour = int((now - ops_start).total_seconds() // 3600)
    hours = [{'label': '%s–%s' % ((ops_start + timedelta(hours=h)).strftime('%H:%M'),
                                  (ops_start + timedelta(hours=h + 1)).strftime('%H:%M')),
              'now': h == live_hour}
             for h in range(24)]

    basis = ([{'kind': 'BARGE', 'stage': lbl, 'p50': barge_table[c][1], 'n': barge_table[c][3]}
              for c, lbl in BARGE_STAGES if c in barge_table] +
             [{'kind': 'MBC', 'stage': lbl, 'p50': mbc_table[c][1], 'n': mbc_table[c][3]}
              for c, lbl in MBC_STAGES if c in mbc_table])

    return {
        'now': now, 'ops_start': ops_start, 'days': days, 'hours': hours,
        'slots': slots, 'overdue': overdue, 'beyond': beyond,
        'at_port': sorted(at_port, key=lambda x: x['arrived_at']),
        'unknown': sorted(unknown, key=lambda x: x['sort_at'], reverse=True),
        'inbound_count': sum(len(c) for row in slots for c in row),
        'basis': basis,
    }


def _busy_stamp():
    """Where the data actually ends — so the selfcheck has live rows to assert
    on even against a restored dump whose data stopped months ago. The 95th
    percentile, not the max: a handful of mistyped future dates would otherwise
    park 'now' in a gap where every real trip is already stale."""
    barges, mbcs = _fetch()
    stamps = [_dt(r.get(c)) for r in barges for c, _l in BARGE_STAGES]
    stamps += [_dt(r.get(c)) for r in mbcs for c, _l in MBC_STAGES]
    epochs = sorted(s.timestamp() for s in stamps if s)
    if not epochs:
        return datetime.now()
    return datetime.fromtimestamp(epochs[int(len(epochs) * 0.95)])


def _demo(now=None):
    """Self-check: run against the live DB and assert the board holds together."""
    b = board(now=now)
    if not b['inbound_count'] and now is None:
        # Every trip is older than STALE_DAYS: rewind to where the data ends so
        # the assertions below actually exercise a populated board.
        return _demo(now=_busy_stamp())
    assert len(b['slots']) == 24 and all(len(r) == HORIZON_DAYS for r in b['slots'])
    assert b['ops_start'].hour == OPS_HOUR
    assert b['ops_start'] <= b['now'] < b['ops_start'] + timedelta(days=1)
    placed = [t for row in b['slots'] for cell in row for t in cell]
    assert len(placed) == b['inbound_count']
    for t in placed:
        assert b['ops_start'] <= t['eta'] < b['ops_start'] + timedelta(days=HORIZON_DAYS)
        assert t['eta_p10'] <= t['eta'] <= t['eta_p90'], 'band inverted for %s' % t['name']
        assert t['eta'] >= t['last_at'], 'ETA before its own milestone'
    for t in b['overdue']:
        assert t['eta'] < b['ops_start']
    names = [(t['kind'], t['name'].upper())
             for t in placed + b['overdue'] + b['beyond'] + b['at_port'] + b['unknown']]
    assert len(names) == len(set(names)), 'a vessel appears on the board twice'
    for row in b['basis']:
        assert row['n'] >= MIN_N and 0 <= row['p50'] <= MAX_H
    print('RP03 selfcheck OK @ %s — %d inbound in %d h, %d overdue, %d at port, %d no-ETA, '
          'basis %d stages' % (b['now'].strftime('%Y-%m-%d %H:%M'), b['inbound_count'],
                               24 * HORIZON_DAYS, len(b['overdue']), len(b['at_port']),
                               len(b['unknown']), len(b['basis'])))


if __name__ == '__main__':
    _demo()
