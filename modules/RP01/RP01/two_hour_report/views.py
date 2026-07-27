"""Two-Hour Discharge Report.

Berth-wise discharge snapshot for a chosen date + time window (e.g. 06:00 to
08:00), combining VCN barges and MBC lines under a single "Barge / MBC"
column per your reference screenshot, with a Balance Qty column showing what
is left against that barge/cargo's trip limit.

Also includes a same-day Shift Wise (A / B / C) discharge comparison at the
bottom of the report — this part ignores the From/To time filter and always
reflects the whole day for the selected date, same as the sample report.

Lives at RP01/2hrs_report/2hrs_report.py. Because BOTH the folder name and
the file name start with a digit, they are not valid Python identifiers —
a plain `from .2hrs_report import 2hrs_report` is a SyntaxError, full stop
(not just the "2hrs_report" after import — the "from .2hrs_report" part is
already invalid on its own). This naming can only be wired up with
importlib. In RP01/__init__.py, replace the normal import line pattern
with:

    import importlib
    _2hrs_report_views = importlib.import_module('.2hrs_report.2hrs_report', package=__name__)

Requirements for this to work:
  - RP01/2hrs_report/ must contain an __init__.py (can be empty) so it's
    a valid Python package, even though its name starts with a digit —
    importlib doesn't care about identifier rules, only the `import`
    keyword's parser does.
  - This module still registers its routes on `bp` the same way every
    other module does (via `from .. import bp` below) — importlib just
    changes *how Python loads the file*, not what the file does once
    loaded.

Template lives at templates/2hrs_report/2hrs_report.html — adjust the
render_template() call below if your templates are laid out differently.

Uses the same lueu_lines / vcn_* / mbc_* tables already used by
LUEU01/model.py — see _balance_qty() below, which mirrors the tier-2 limit
logic in that file's _resolve_trip_limits().
"""

from flask import render_template, request, session, redirect, url_for, jsonify
from functools import wraps
from datetime import datetime
import re

from .. import bp
from database import get_db, get_cursor


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def _natural_sort_key(value):
    """'Berth 9' before 'Berth 10', etc."""
    text = str(value or '').strip()
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', text)]


def _norm_barge_label(value):
    """Collapse whitespace around the '/' separator so 'Barge/1' and
    'Barge / 1' compare equal. Different parts of the app build this label
    with different spacing (CONCAT(barge_name, '/', trip) vs
    f"{barge_name} / {trip}"), so every comparison must go through this
    normalizer or barge-specific balance matches silently fail and fall
    back to the wrong tier."""
    return re.sub(r'\s*/\s*', '/', (value or '').strip())


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@bp.route('/module/RP01/two-hour-report/')
@login_required
def two_hour_report_index():
    return render_template('two_hour_report/two_hour_report.html',
                            username=session.get('username'))


# ---------------------------------------------------------------------------
# Balance-qty helper (mirrors LUEU01/model.py's tier-2 limit logic)
# ---------------------------------------------------------------------------

def _balance_qty(cur, source_type, source_id, barge_name, cargo_name):
    """Remaining quantity against the tightest applicable trip limit.

    VCN -> barge/trip discharge_quantity from LDUD (tier 2), falling back to
           the whole-vessel cargo-declaration BL (tier 1) if there's no
           barge-specific match.
    MBC -> this cargo's customer-details quantity (tier 2), falling back to
           the header BL quantity (tier 1) if there's no cargo-specific match.

    Handled sums include every non-deleted lueu_lines row for that scope —
    this is a live, all-time balance, independent of the report's time
    window (matches the "Bal Qty." column in the reference report: total
    trip qty minus everything discharged against it so far, including the
    row just reported).
    Returns None when there is no basis to compare against.
    """
    if not source_type or not source_id:
        return None

    if source_type == 'VCN':
        expected = None
        if barge_name:
            target = _norm_barge_label(barge_name)
            cur.execute('SELECT id FROM ldud_header WHERE vcn_id = %s', [source_id])
            ldud = cur.fetchone()
            if ldud:
                cur.execute('''
                    SELECT barge_name, trip_number,
                           COALESCE(SUM(discharge_quantity), 0) AS expected_qty
                    FROM ldud_barge_lines
                    WHERE ldud_id = %s AND barge_name IS NOT NULL AND barge_name != ''
                    GROUP BY barge_name, trip_number
                ''', [ldud['id']])
                for r in cur.fetchall():
                    trip = r['trip_number'] or ''
                    display = f"{r['barge_name']}/{trip}" if trip else r['barge_name']
                    if _norm_barge_label(display) == target:
                        expected = float(r['expected_qty'] or 0)
                        break
            if expected is not None and expected > 0:
                cur.execute('''
                    SELECT COALESCE(SUM(quantity), 0) AS handled
                    FROM lueu_lines
                    WHERE source_type = 'VCN' AND source_id = %s AND barge_name = %s
                      AND (is_deleted IS NOT TRUE)
                ''', [source_id, barge_name])
                handled = float(cur.fetchone()['handled'] or 0)
                return round(expected - handled, 3)

        # Fallback: whole-vessel BL (import + export cargo declarations)
        cur.execute('''
            SELECT COALESCE((SELECT SUM(bl_quantity) FROM vcn_cargo_declaration WHERE vcn_id = %s), 0)
                 + COALESCE((SELECT SUM(bl_quantity) FROM vcn_export_cargo_declaration WHERE vcn_id = %s), 0)
                   AS vessel_bl
        ''', [source_id, source_id])
        vessel_bl = float(cur.fetchone()['vessel_bl'] or 0)
        if vessel_bl <= 0:
            return None
        cur.execute('''
            SELECT COALESCE(SUM(quantity), 0) AS handled
            FROM lueu_lines
            WHERE source_type = 'VCN' AND source_id = %s AND (is_deleted IS NOT TRUE)
        ''', [source_id])
        handled = float(cur.fetchone()['handled'] or 0)
        return round(vessel_bl - handled, 3)

    if source_type == 'MBC':
        if cargo_name:
            cur.execute('''
                SELECT COALESCE(SUM(quantity), 0) AS cargo_qty
                FROM mbc_customer_details
                WHERE mbc_id = %s AND cargo_name = %s
            ''', [source_id, cargo_name])
            cargo_qty = float(cur.fetchone()['cargo_qty'] or 0)
            if cargo_qty > 0:
                cur.execute('''
                    SELECT COALESCE(SUM(quantity), 0) AS handled
                    FROM lueu_lines
                    WHERE source_type = 'MBC' AND source_id = %s AND cargo_name = %s
                      AND (is_deleted IS NOT TRUE)
                ''', [source_id, cargo_name])
                handled = float(cur.fetchone()['handled'] or 0)
                return round(cargo_qty - handled, 3)

        cur.execute('SELECT COALESCE(bl_quantity, 0) AS bl FROM mbc_header WHERE id = %s', [source_id])
        row = cur.fetchone()
        header_bl = float(row['bl'] or 0) if row else 0.0
        if header_bl <= 0:
            return None
        cur.execute('''
            SELECT COALESCE(SUM(quantity), 0) AS handled
            FROM lueu_lines
            WHERE source_type = 'MBC' AND source_id = %s AND (is_deleted IS NOT TRUE)
        ''', [source_id])
        handled = float(cur.fetchone()['handled'] or 0)
        return round(header_bl - handled, 3)

    return None


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _fetch_two_hour_rows(entry_date, from_time, to_time, cargo=''):
    conn = get_db()
    cur = get_cursor(conn)

    # Main report query
    # Removed the `OR (source_id IS NOT NULL)` branch from the previous
    # fix — that condition matched generic "Idle" equipment rows that have
    # a source_id but NO barge_name/cargo_name at all, which is why blank
    # "—" / "—" rows were showing up. A row now only counts as a real
    # "waiting on berth" entry if it has an actual discharge quantity, OR
    # an identifiable barge_name, OR an identifiable cargo_name.
    cur.execute("""
        SELECT
            berth_name,
            barge_name,
            cargo_name,
            source_type,
            source_id,
            COALESCE(SUM(quantity),0) AS disch_qty
        FROM lueu_lines
        WHERE entry_date = %s
          AND (is_deleted IS NOT TRUE)
          AND berth_name IS NOT NULL
          AND berth_name <> ''
          AND from_time >= %s
          AND from_time < %s
          AND (
                (quantity IS NOT NULL AND quantity >= 0)
                OR (barge_name IS NOT NULL AND barge_name <> '')
                OR (cargo_name IS NOT NULL AND cargo_name <> '')
              )
          AND (%s = '' OR cargo_name = %s)
        GROUP BY
            berth_name,
            barge_name,
            cargo_name,
            source_type,
            source_id
        ORDER BY
            berth_name,
            barge_name
    """, [
        entry_date,
        from_time,
        to_time,
        cargo,
        cargo
    ])

    rows = [dict(r) for r in cur.fetchall()]

    for r in rows:

        # Delay query
        cur.execute("""
            SELECT
                l.delay_name,
                COALESCE(d.type,'Other') AS delay_type,
                l.from_time,
                l.to_time
            FROM lueu_lines l
            LEFT JOIN port_delay_types d
                   ON d.name = l.delay_name
            WHERE l.entry_date=%s
              AND l.from_time >= %s
              AND l.from_time < %s
              AND l.source_type=%s
              AND l.source_id=%s
              AND COALESCE(l.barge_name,'')=COALESCE(%s,'')
              AND COALESCE(l.cargo_name,'')=COALESCE(%s,'')
              AND l.is_deleted IS NOT TRUE
              AND l.delay_name IS NOT NULL
              AND (
                d.type IN ('RMHS Delays', 'Maintenance Delays')
                OR l.delay_name = 'No Dumper'
            )
            ORDER BY d.type,l.from_time
        """, [
            entry_date,
            from_time,
            to_time,
            r['source_type'],
            r['source_id'],
            r['barge_name'],
            r['cargo_name']
        ])

        from collections import defaultdict

        delay_totals = defaultdict(int)
        # Rows whose to_time (or from_time) came back NULL — an open /
        # not-yet-closed delay. We can't compute a duration for these, so
        # they're tracked separately and flagged in the label instead of
        # crashing strptime() or being silently dropped.
        open_delay_flags = set()

        for d in cur.fetchall():

            key = (d["delay_type"], d["delay_name"])

            if not d["from_time"] or not d["to_time"]:
                delay_totals.setdefault(key, 0)
                open_delay_flags.add(key)
                continue

            start = datetime.strptime(d["from_time"], "%H:%M")
            end = datetime.strptime(d["to_time"], "%H:%M")

            mins = int((end - start).total_seconds() // 60)
            if mins < 0:
                mins += 24 * 60

            delay_totals[key] += mins

        delay_list = []

        for (delay_type, delay_name), total_mins in delay_totals.items():

            hrs = total_mins // 60
            mins = total_mins % 60

            duration = f"{hrs:02d}:{mins:02d}"
            if (delay_type, delay_name) in open_delay_flags:
                duration += "+ (ongoing)"

            delay_list.append(
                f"{delay_type}: {delay_name} ({duration})"
            )

        r["delay_name"] = " / ".join(delay_list) if delay_list else "—"

        r["bal_qty"] = _balance_qty(
            cur,
            r["source_type"],
            r["source_id"],
            r["barge_name"],
            r["cargo_name"]
        )

    conn.close()
    return rows

def _fetch_shift_totals(entry_date):
    """Same-day A/B/C discharge totals, independent of the time-window filter —
    matches the 'A Shift / B Shift / C Shift / Total Discharge' block at the
    bottom of the reference report."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT shift, COALESCE(SUM(quantity), 0) AS qty
        FROM lueu_lines
        WHERE entry_date = %s AND (is_deleted IS NOT TRUE)
        GROUP BY shift
    ''', [entry_date])
    totals = {'A': 0.0, 'B': 0.0, 'C': 0.0}
    for r in cur.fetchall():
        s = (r['shift'] or '').strip().upper()
        if s in totals:
            totals[s] = float(r['qty'] or 0)
    conn.close()
    return {
        'A': round(totals['A'], 2),
        'B': round(totals['B'], 2),
        'C': round(totals['C'], 2),
        'total': round(sum(totals.values()), 2),
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@bp.route('/api/module/RP01/two-hour-report/preview')
@login_required
def two_hour_report_preview():
    entry_date = request.args.get('entry_date', '')
    from_time = request.args.get('from_time', '')
    to_time = request.args.get('to_time', '')
    if not entry_date or not from_time or not to_time:
        return jsonify({'error': 'entry_date, from_time and to_time are required'}), 400

    cargo = request.args.get("cargo", "")

    rows = _fetch_two_hour_rows(
        entry_date,
        from_time,
        to_time,
        cargo
    )

    berths = {}
    order = []
    for r in rows:
        b = r['berth_name']
        if b not in berths:
            berths[b] = {'berth': b, 'throughput': 0.0, 'lines': []}
            order.append(b)
        berths[b]['throughput'] += float(r['disch_qty'] or 0)
        berths[b]['lines'].append({
            'barge_name': r.get('barge_name') or '—',
            'cargo_name': r.get('cargo_name') or '—',
            'delay_name': r.get('delay_name') or '—',
            'disch_qty': round(float(r['disch_qty'] or 0), 2),
            'bal_qty': r['bal_qty'],  # None -> render as '—' on the client
        })

    order.sort(key=_natural_sort_key)
    berth_rows = []
    grand_total = 0.0
    # NEW: running total of Bal Qty across every line in the report, so the
    # Total row at the bottom can show a Bal Qty total the same way it
    # already shows a Disch Qty total. None values (no basis to compare
    # against) are skipped rather than treated as 0, so they don't distort
    # the sum.
    grand_total_bal = 0.0
    for b in order:
        entry = berths[b]
        entry['throughput'] = round(entry['throughput'], 2)
        grand_total += entry['throughput']
        for line in entry['lines']:
            if line['bal_qty'] is not None:
                grand_total_bal += float(line['bal_qty'])
        berth_rows.append(entry)

    shift_totals = _fetch_shift_totals(entry_date)

    try:
        date_label = datetime.strptime(entry_date, '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception:
        date_label = entry_date
     
     
    cargo_options = sorted({
            r["cargo_name"]
            for r in rows
            if r.get("cargo_name")
        })   

    return jsonify({
        'title': f'{from_time} TO {to_time} Discharge Report',
        'date_label': date_label,
        'entry_date': entry_date,
        'from_time': from_time,
        'to_time': to_time,
        'berths': berth_rows,
        'grand_total': round(grand_total, 2),
        'grand_total_bal': round(grand_total_bal, 2),  # NEW
        'shift_totals': shift_totals,
        'cargo_options': cargo_options
    })