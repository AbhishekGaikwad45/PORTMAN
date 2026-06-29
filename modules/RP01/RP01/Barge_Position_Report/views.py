from flask import render_template, session, redirect, url_for, jsonify, request
from datetime import date, datetime, timedelta
from functools import wraps
from database import get_db, get_cursor
from .. import bp
from io import BytesIO
from flask import send_file
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def _classify_tide_types(rows):
    if not rows:
        return []
    heights = [float(r.get('tide_meters') or 0) for r in rows]
    current = 'HW' if len(heights) >= 2 and heights[0] > heights[1] else 'LW'
    types = []
    for _ in heights:
        types.append(current)
        current = 'LW' if current == 'HW' else 'HW'
    return types

def _parse_dt(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).strip())
    except Exception:
        try:
            return datetime.strptime(str(val).strip(), '%Y-%m-%d %H:%M:%S')
        except Exception:
            return None

def _fmt_dt(val, strfmt='%d-%m-%Y %H:%M'):
    dt = _parse_dt(val)
    return dt.strftime(strfmt) if dt else ''

def _fetch_mother_vessels(from_datetime, to_datetime):

    conn = get_db()
    cur  = get_cursor(conn)

    cur.execute("""
        SELECT
            h.id,
            h.vcn_id,
            h.vessel_name,
            h.operation_type,
            h.nor_tendered,

            (
                SELECT MIN(a.discharge_started)
                FROM ldud_anchorage a
                WHERE a.ldud_id = h.id
            ) AS discharge_commenced,

            (
                SELECT MAX(a.discharge_commenced)
                FROM ldud_anchorage a
                WHERE a.ldud_id = h.id
            ) AS discharge_completed

        FROM ldud_header h
        WHERE h.nor_tendered IS NOT NULL
        ORDER BY h.nor_tendered
    """)

    all_vessels = [dict(r) for r in cur.fetchall()]

    vessels = []
    for v in all_vessels:
        commenced = _parse_dt(v.get("discharge_commenced"))
        completed = _parse_dt(v.get("discharge_completed"))

        if not commenced:
            continue

        if commenced > to_datetime:
            continue

        if completed and completed < from_datetime:
            continue

        vessels.append(v)

    ldud_ids = [v['id'] for v in vessels]
    vcn_ids  = [v['vcn_id'] for v in vessels if v.get('vcn_id')]

    bl_import        = {}
    bl_export        = {}
    vcn_meta         = {}
    ops_24h          = {}
    ops_till         = {}
    under_loading    = {}
    at_gull_loaded   = {}
    eta_to_dharamtar = {}
    mbc_eta_list     = []

    report_date = to_datetime.date()
    prev_date   = report_date - timedelta(days=1)

    if ldud_ids:
        cur.execute("""
            SELECT ldud_id, COALESCE(SUM(quantity),0) qty
            FROM ldud_vessel_operations
            WHERE ldud_id = ANY(%s)
              AND TO_DATE(start_time,'YYYY-MM-DD') = %s
            GROUP BY ldud_id
        """, (ldud_ids, prev_date))
        for r in cur.fetchall():
            ops_24h[r['ldud_id']] = float(r['qty'])

    if ldud_ids:
        cur.execute("""
            SELECT ldud_id, COALESCE(SUM(quantity),0) qty
            FROM ldud_vessel_operations
            WHERE ldud_id = ANY(%s)
              AND TO_DATE(start_time,'YYYY-MM-DD') <= %s
            GROUP BY ldud_id
        """, (ldud_ids, prev_date))
        for r in cur.fetchall():
            ops_till[r['ldud_id']] = float(r['qty'])

    if vcn_ids:
        cur.execute("""
            SELECT vcn_id, COALESCE(SUM(bl_quantity),0) total
            FROM vcn_cargo_declaration
            WHERE vcn_id = ANY(%s) GROUP BY vcn_id
        """, (vcn_ids,))
        bl_import = {r['vcn_id']: float(r['total']) for r in cur.fetchall()}

        cur.execute("""
            SELECT vcn_id, COALESCE(SUM(bl_quantity),0) total
            FROM vcn_export_cargo_declaration
            WHERE vcn_id = ANY(%s) GROUP BY vcn_id
        """, (vcn_ids,))
        bl_export = {r['vcn_id']: float(r['total']) for r in cur.fetchall()}

        cur.execute("""
            SELECT id, importer_exporter_name
            FROM vcn_header WHERE id = ANY(%s)
        """, (vcn_ids,))
        vcn_meta = {r['id']: r['importer_exporter_name'] or '' for r in cur.fetchall()}

    if ldud_ids:
        cur.execute("""
            SELECT ldud_id,
                   STRING_AGG(TRIM(barge_name), ', ' ORDER BY barge_name) AS barges
            FROM ldud_barge_lines
            WHERE commenced_loading IS NOT NULL
              AND completed_loading IS NULL
              AND ldud_id = ANY(%s)
            GROUP BY ldud_id
        """, (ldud_ids,))
        under_loading = {r['ldud_id']: r['barges'] for r in cur.fetchall()}

        cur.execute("""
            SELECT ldud_id,
                   STRING_AGG(TRIM(barge_name), ', ' ORDER BY barge_name) AS barges
            FROM ldud_barge_lines
            WHERE cast_off_mv IS NOT NULL
              AND (along_side_berth IS NULL OR TRIM(COALESCE(along_side_berth,'')) = '')
              AND ldud_id = ANY(%s)
            GROUP BY ldud_id
        """, (ldud_ids,))
        at_gull_loaded = {r['ldud_id']: r['barges'] for r in cur.fetchall()}

        cur.execute("""
            SELECT ldud_id,
                STRING_AGG(TRIM(barge_name), ', ' ORDER BY barge_name) AS barges
            FROM ldud_barge_lines
            WHERE anchored_gull_island IS NOT NULL
            AND cast_off_port IS NULL
            AND ldud_id = ANY(%s)
            GROUP BY ldud_id
        """, (ldud_ids,))
        eta_to_dharamtar = {r['ldud_id']: r['barges'] for r in cur.fetchall()}

    cur.execute("""
        SELECT
            h.mbc_name,
            p.departure_gull_island,
            h.cargo_name,
            COALESCE(h.bl_quantity, 0) AS bl_qty
        FROM mbc_header h
        JOIN mbc_discharge_port_lines p ON p.mbc_id = h.id
        WHERE p.departure_gull_island IS NOT NULL
          AND TRIM(COALESCE(p.departure_gull_island, '')) <> ''
          AND (
                p.vessel_arrival_port IS NULL
                OR TRIM(COALESCE(p.vessel_arrival_port, '')) = ''
              )
          AND NULLIF(TRIM(p.departure_gull_island), '')::timestamp <= %s
        ORDER BY p.departure_gull_island
    """, (to_datetime,))
 
    mbc_eta_rows = cur.fetchall()
 
    # Format: "MBC_NAME (cargo) ETA: departure_time"
    mbc_eta_list = []
    for r in mbc_eta_rows:
        dep_dt = _parse_dt(r['departure_gull_island'])
        dep_str = dep_dt.strftime('%d-%m %H:%M') if dep_dt else ''
        entry = f"{r['mbc_name']} ({r['cargo_name'] or ''}) - Dep Gull: {dep_str}"
        mbc_eta_list.append(entry)

    cur.close()
    conn.close()

    for i, v in enumerate(vessels):
        vid    = v.get('vcn_id')
        op     = v.get('operation_type', '')
        bl_qty = (bl_export.get(vid, 0) if op == 'Export' else bl_import.get(vid, 0))

        v['stevedore_group']  = vcn_meta.get(vid, '')
        v['bl_qty']           = bl_qty
        v['ops_24h']          = ops_24h.get(v['id'], 0)
        v['ops_till']         = ops_till.get(v['id'], 0)
        v['balance']          = round(bl_qty - ops_till.get(v['id'], 0), 2)
        v['under_loading']    = under_loading.get(v['id'], '')
        v['eta_to_dharamtar'] = eta_to_dharamtar.get(v['id'], '')
        v['wt_r19']           = ''
        v['at_gull_loaded']   = at_gull_loaded.get(v['id'], '')
        if i < len(mbc_eta_list):
          v['mbc_eta'] = mbc_eta_list[i]
        else:
            v['mbc_eta'] = ''

    return vessels



def _fetch_tide_data(from_datetime, to_datetime):

    conn = get_db()
    cur = get_cursor(conn)

    cur.execute("""
        SELECT
            tide_datetime,
            tide_meters
        FROM tide_master
        WHERE
            tide_datetime IS NOT NULL
            AND TRIM(tide_datetime) <> ''
            AND NULLIF(TRIM(tide_datetime), '')::timestamp >= %s
        ORDER BY NULLIF(TRIM(tide_datetime), '')::timestamp
        LIMIT 6
    """, (from_datetime,))

    rows = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()

    types = _classify_tide_types(rows)

    tide_data = []

    for row, tide_type in zip(rows, types):
        dt = _parse_dt(row["tide_datetime"])

        tide_data.append({
            "type": tide_type,
            "time": dt.strftime("%d/%m %H:%M") if dt else "",
            "height": row["tide_meters"],
        })

    return tide_data




def get_shift_code(dt):
    if not dt:
        return None
    hour = dt.hour
    if 6 <= hour < 14:
        return "A"
    elif 14 <= hour < 22:
        return "B"
    else:
        return "C"


def _fetch_all_barges(selected_date=None, selected_shift="ALL"):

    conn = get_db()
    cur  = get_cursor(conn)

    barges = []
    occupied_berth_set = set()

    selected_dt = None
    if selected_date:
        try:
            selected_dt = datetime.strptime(selected_date, "%Y-%m-%d").date()
        except Exception:
            pass

    cur.execute("""
        WITH discharge_sums AS (
            SELECT
                TRIM(UPPER(ll.barge_name)) AS barge_name,
                ll.source_id,
                SUM(COALESCE(ll.quantity,0)) AS discharged_qty
            FROM lueu_lines ll
            WHERE ll.is_deleted IS NOT TRUE
            AND ll.source_type = 'VCN'
            GROUP BY TRIM(UPPER(ll.barge_name)), ll.source_id
        )
        SELECT
            l.id,
            l.barge_name,
            l.trip_number,
            l.cargo_name,
            l.cast_off_port,
            l.along_side_berth,
            l.commence_discharge_berth,
            l.completed_discharge_berth,
            COALESCE(l.discharge_quantity,0) AS discharge_qty,
            (
                COALESCE(l.discharge_quantity,0)
                - COALESCE(ds.discharged_qty,0)
            ) AS balance_qty
        FROM ldud_barge_lines l
        LEFT JOIN ldud_header h
            ON h.id = l.ldud_id
        LEFT JOIN discharge_sums ds
            ON ds.barge_name = TRIM(
                UPPER(
                    CONCAT(
                        l.barge_name,
                        ' / ',
                        COALESCE(l.trip_number::text,'1')
                    )
                )
            )
            AND ds.source_id = h.vcn_id
        WHERE COALESCE(TRIM(l.barge_name),'') <> ''
    """)
    for row in cur.fetchall():
        row       = dict(row)

        
        cutoff_date = date(2026, 5, 1)

        if selected_dt and selected_dt < cutoff_date:
            continue

        # Skip completed
        if row.get("cast_off_port") and str(row.get("cast_off_port")).strip():
            continue

        if row.get("completed_discharge_berth") and str(row.get("completed_discharge_berth")).strip():
            continue

        # Waiting — alongside berth set, discharge not started
        if (
            row.get("along_side_berth")
            and str(row.get("along_side_berth")).strip()
            and (
                row.get("commence_discharge_berth") is None
                or str(row.get("commence_discharge_berth")).strip() == ""
            )
        ):
            status = "Waiting"

        # Under Discharge — discharge started, not completed, balance > 0
        elif (
            row.get("commence_discharge_berth")
            and str(row.get("commence_discharge_berth")).strip()
            and (
                row.get("completed_discharge_berth") is None
                or str(row.get("completed_discharge_berth")).strip() == ""
            )
            and float(row.get("balance_qty", 0) or 0) > 0
        ):
            status = "Under Discharge"

        else:
            continue

        # ← NO date filter, NO shift filter for barges

        completed_date = _fmt_dt(row.get("cast_off_port")) if row.get("cast_off_port") else None
        berth = (row.get("commence_discharge_berth") or row.get("along_side_berth") or "")

        barges.append({
            "id":             row["id"],
            "type":           "BARGE",
            "completed_date": completed_date,
            "barge_name":     row["barge_name"],
            "name":           row["barge_name"],
            "cargo": row.get("cargo_name") or row.get("cargo_type") or "",
            "qty":            row["discharge_qty"],
            "discharge_qty":  float(row["discharge_qty"]),
            "total_qty":      float(row["discharge_qty"]),
            "balance_qty": float(row.get("balance_qty", 0) or 0),
            "berth":          berth,
            "status":         status,
            "commence_discharge_berth": str(row.get("commence_discharge_berth") or "").strip(),
            "unloading_commenced":      "",   # barges use commence_discharge_berth
        })

    cur.execute("""
        WITH latest_mbc AS (
            SELECT
                h.id,
                h.mbc_name,
                h.cargo_name,
                COALESCE(h.bl_quantity,0) AS bl_qty,
                p.vessel_unloading_berth AS berth,
                p.vessel_arrival_port AS arrival_port,
                p.unloading_commenced,
                p.unloading_completed,
                p.vessel_cast_off AS mbc_cast_off,
                ROW_NUMBER() OVER (
                    PARTITION BY h.mbc_name
                    ORDER BY p.id DESC
                ) rn
            FROM mbc_header h
            JOIN mbc_discharge_port_lines p
                ON p.mbc_id = h.id
        )
        SELECT
            m.*,
            COALESCE(l.actual_qty,0) AS actual_qty
        FROM latest_mbc m
        LEFT JOIN (
            SELECT
                source_id,
                SUM(COALESCE(quantity,0)) AS actual_qty
            FROM lueu_lines
            WHERE source_type = 'MBC'
            AND is_deleted IS NOT TRUE
            GROUP BY source_id
        ) l ON l.source_id = m.id
        WHERE m.rn = 1
        AND m.arrival_port IS NOT NULL
        AND TRIM(COALESCE(m.arrival_port,'')) <> ''
        AND (
                m.unloading_completed IS NULL
                OR TRIM(COALESCE(m.unloading_completed,'')) = ''
            )
        AND (
                m.mbc_cast_off IS NULL
                OR TRIM(COALESCE(m.mbc_cast_off,'')) = ''
            )
        ORDER BY m.mbc_name
    """)
    for row in cur.fetchall():
        row = dict(row)

        cutoff_date = date(2026, 5, 1)

        if selected_dt and selected_dt < cutoff_date:
            continue

        berth = (row.get("berth") or "").strip()

        # Waiting
        if (
            row.get("arrival_port")
            and str(row.get("arrival_port")).strip()
            and (
                row.get("unloading_commenced") is None
                or str(row.get("unloading_commenced")).strip() == ""
            )
        ):
            status = "Waiting"

        # Discharging
        elif (
            row.get("unloading_commenced")
            and str(row.get("unloading_commenced")).strip()
        ):
            status = "Discharging"

        else:
            continue

        bl_qty = float(row["bl_qty"] or 0)
        actual_qty = float(row["actual_qty"] or 0)
        balance_qty = max(bl_qty - actual_qty, 0)
        # Hide fully discharged MBC
        if balance_qty <= 0:
            continue

        barges.append({
            "id": row["id"],
            "type": "MBC",
            "completed_date": _fmt_dt(row.get("unloading_completed")),
            "barge_name": row["mbc_name"],
            "name": row["mbc_name"],
            "cargo": row.get("cargo_name") or "",
            "qty": bl_qty,
            "discharge_qty": bl_qty,
            "total_qty": bl_qty,
            "balance_qty": balance_qty,
            "berth": "",
            "status": status,
            "unloading_commenced":      str(row.get("unloading_commenced") or "").strip(),
            "commence_discharge_berth": "",   # MBCs use unloading_commenced
        })

        if status == "Discharging" and berth:
            occupied_berth_set.add(berth)

    # Sort alphabetically by name regardless of type
    barges.sort(key=lambda x: (x["name"] or "").upper())

    cur.close()
    conn.close()
    return barges, occupied_berth_set


# ── ROUTES — each defined exactly ONCE ───────────────────────────────────────

@bp.route('/module/RP01/bargeposition/')
@login_required
def bargeposition():

    barges, occupied_berth_set = _fetch_all_barges()

    waiting = [
    b for b in barges
    if b["status"] in ["Waiting", "Under Discharge"]
    ]

    discharging = [
        b for b in barges
        if b["status"] == "Discharging"
    ]
    occupied_berths = len(occupied_berth_set)

    today         = date.today()
    today_str     = today.strftime('%Y-%m-%d')

    from_date_str = request.args.get('from_date', today_str)
    from_time_str = request.args.get('from_time', '00:00')
    to_date_str   = request.args.get('to_date',   today_str)
    to_time_str   = request.args.get('to_time',   '23:59')

    to_datetime   = datetime.strptime(f"{to_date_str} {to_time_str}", '%Y-%m-%d %H:%M')

    # Widen window: start from previous day 00:00 so vessels that
    # completed early on the selected date are still included
    from_datetime = datetime.strptime(from_date_str, '%Y-%m-%d') - timedelta(days=1)
    from_datetime = from_datetime.replace(hour=0, minute=0, second=0)

    mother_vessels_raw = _fetch_mother_vessels(from_datetime, to_datetime)

    mother_vessels = [{
        'vessel_name':         v.get('vessel_name') or '',
        'stevedore_group':     v.get('stevedore_group') or '',
        'bl_qty':              v.get('bl_qty') or 0,
        'ops_24h':             v.get('ops_24h') or 0,
        'ops_till':            v.get('ops_till') or 0,
        'balance':             v.get('balance') or 0,
        'under_loading':       v.get('under_loading') or '',
        'eta_to_dharamtar':    v.get('eta_to_dharamtar') or '',
        'wt_r19':              v.get('wt_r19') or '',
        'at_gull_loaded':      v.get('at_gull_loaded') or '',
        'mbc_eta':             v.get('mbc_eta') or '',
        'nor_tendered':        _fmt_dt(v.get('nor_tendered')),
        'discharge_commenced': _fmt_dt(v.get('discharge_commenced')),
        'discharge_completed': _fmt_dt(v.get('discharge_completed')),
        'unloaded_till_date':  '',
        'disch_commenced':     '',
    } for v in mother_vessels_raw]
    
    conn = get_db()
    cur = get_cursor(conn)



    cur.execute("""
    SELECT berth_name
    FROM port_berth_master
    """)

    berths = [r["berth_name"].upper() for r in cur.fetchall()]

    old_berths = [
        "BERTH 1",
        "BERTH 2",
        "BERTH 3",
        "BERTH 4",
        "BERTH 5",
        "BERTH 5A",
    ]

    new_berths = [
        "BERTH 6",
        "BERTH 7",
        "BERTH 8",
        "BERTH 8A",
        "BERTH 9",
        "BERTH 10",
        "BERTH 11",
        "BERTH 12",
    ]

    cur.close()
    conn.close()

    tide_data = _fetch_tide_data(from_datetime, to_datetime)

    return render_template(
        "Barge_Position_Report/barge_dashboard.html",
        waiting=waiting,
        discharging=discharging,
        all_barges=barges,
        mother_vessels=mother_vessels,
        tide_data=tide_data,
         berths=berths,
        old_berths=old_berths,
        new_berths=new_berths,
        from_date=from_date_str,
        from_time=from_time_str,
        to_date=to_date_str,
        to_time=to_time_str,
        total_barges=len(barges),
        waiting_count=len(waiting),
        discharging_count=len(discharging),
        occupied_berths=occupied_berths,
        available_berths=max(0, 14 - occupied_berths),
        
    )
    
# ── API ROUTES ────────────────────────────────────────────────────────────────

@bp.route('/api/module/RP01/berth-occupancy')
@login_required
def api_berth_occupancy():
    include_completed = request.args.get('completed', '0') == '1'

    if include_completed:
        report_date = request.args.get('date', '')
        shift       = request.args.get('shift', 'ALL')

        if not report_date:
            return jsonify([])

        try:
            base = datetime.strptime(report_date, '%Y-%m-%d')
        except Exception:
            return jsonify([])

        # For date-only filtering — match the full selected date regardless of time
        date_start = base.replace(hour=0,  minute=0,  second=0)
        date_end   = base.replace(hour=23, minute=59, second=59)

        # If a specific shift is selected, narrow the window
        if shift.upper() != 'ALL':
            SHIFT_WINDOWS = {
                'A': (base.replace(hour=6,  minute=0),  base.replace(hour=14, minute=0)),
                'B': (base.replace(hour=14, minute=0),  base.replace(hour=22, minute=0)),
                'C': (base.replace(hour=22, minute=0),  (base + timedelta(days=1)).replace(hour=6, minute=0)),
            }
            from_dt, to_dt = SHIFT_WINDOWS.get(shift.upper(), (date_start, date_end))
        else:
            from_dt, to_dt = date_start, date_end

        conn = get_db()
        cur  = get_cursor(conn)
        results = []

        # Completed BARGES
        # Use flexible regex: matches YYYY-MM-DD with space OR T separator
        cur.execute(r"""
            SELECT *
            FROM (
                SELECT
                    bl.barge_name,
                    bl.cargo_name,
                    bl.commence_discharge_berth,
                    bl.along_side_berth,
                    bl.completed_discharge_berth,
                    bl.cast_off_port,
                    COALESCE(bl.discharge_quantity, 0) AS bl_qty,
                    CASE
                        WHEN bl.cast_off_port IS NOT NULL
                             AND TRIM(bl.cast_off_port) ~ '^\d{4}-\d{2}-\d{2}[T ]'
                        THEN SUBSTRING(TRIM(bl.cast_off_port), 1, 10)::date
                        WHEN bl.completed_discharge_berth IS NOT NULL
                             AND TRIM(bl.completed_discharge_berth) ~ '^\d{4}-\d{2}-\d{2}[T ]'
                        THEN SUBSTRING(TRIM(bl.completed_discharge_berth), 1, 10)::date
                        ELSE NULL
                    END AS completed_date
                FROM ldud_barge_lines bl
                JOIN ldud_header h ON h.id = bl.ldud_id
                WHERE COALESCE(TRIM(bl.barge_name),'') <> ''
                  AND (
                        (bl.cast_off_port IS NOT NULL
                         AND TRIM(bl.cast_off_port) ~ '^\d{4}-\d{2}-\d{2}[T ]')
                        OR
                        (bl.completed_discharge_berth IS NOT NULL
                         AND TRIM(bl.completed_discharge_berth) ~ '^\d{4}-\d{2}-\d{2}[T ]')
                      )
            ) sub
            WHERE sub.completed_date = %s::date
            ORDER BY sub.barge_name
        """, (report_date,))

        for row in cur.fetchall():
            row = dict(row)
            commenced = _fmt_dt(row.get('commence_discharge_berth') or row.get('along_side_berth'))
            completed = _fmt_dt(row.get('cast_off_port') or row.get('completed_discharge_berth'))
            results.append({
                'type':      'BARGE',
                'status':    'Completed',
                'name':      row['barge_name'],
                'cargo':     row.get('cargo_name') or '',
                'bl_qty':    float(row['bl_qty'] or 0),
                'commenced': commenced,
                'completed': completed,
            })

        # Completed MBCs
        cur.execute(r"""
            SELECT *
            FROM (
                SELECT
                    h.mbc_name,
                    h.cargo_name,
                    COALESCE(h.bl_quantity, 0) AS bl_qty,
                    p.unloading_commenced,
                    p.unloading_completed,
                    p.vessel_cast_off,
                    CASE
                        WHEN p.unloading_completed IS NOT NULL
                             AND TRIM(p.unloading_completed) ~ '^\d{4}-\d{2}-\d{2}[T ]'
                        THEN SUBSTRING(TRIM(p.unloading_completed), 1, 10)::date
                        WHEN p.vessel_cast_off IS NOT NULL
                             AND TRIM(p.vessel_cast_off) ~ '^\d{4}-\d{2}-\d{2}[T ]'
                        THEN SUBSTRING(TRIM(p.vessel_cast_off), 1, 10)::date
                        ELSE NULL
                    END AS completed_date
                FROM mbc_header h
                JOIN mbc_discharge_port_lines p ON p.mbc_id = h.id
                WHERE (
                        (p.unloading_completed IS NOT NULL
                         AND TRIM(p.unloading_completed) ~ '^\d{4}-\d{2}-\d{2}[T ]')
                        OR
                        (p.vessel_cast_off IS NOT NULL
                         AND TRIM(p.vessel_cast_off) ~ '^\d{4}-\d{2}-\d{2}[T ]')
                      )
            ) sub
            WHERE sub.completed_date = %s::date
            ORDER BY sub.mbc_name
        """, (report_date,))

        for row in cur.fetchall():
            row = dict(row)
            results.append({
                'type':      'MBC',
                'status':    'Completed',
                'name':      row['mbc_name'],
                'cargo':     row.get('cargo_name') or '',
                'bl_qty':    float(row['bl_qty'] or 0),
                'commenced': _fmt_dt(row.get('unloading_commenced')),
                'completed': _fmt_dt(row.get('unloading_completed') or row.get('vessel_cast_off')),
            })

        cur.close()
        conn.close()
        return jsonify(results)

    # Original logic — completely untouched
    barges, _ = _fetch_all_barges()
    return jsonify(barges)


@bp.route('/api/module/RP01/mother-vessel-data')
@login_required
def api_mother_vessel_data():
    from_datetime = _parse_dt(request.args.get('from_datetime'))
    to_datetime   = _parse_dt(request.args.get('to_datetime'))
    if not from_datetime or not to_datetime:
        return jsonify([])
    vessels_raw = _fetch_mother_vessels(from_datetime, to_datetime)
    vessels = [{
        'vessel_name':         v.get('vessel_name') or '',
        'discharge_commenced': _fmt_dt(v.get('discharge_commenced')),
        'discharge_completed': _fmt_dt(v.get('discharge_completed')),
        'under_loading':       v.get('under_loading') or '',
        'eta_to_dharamtar':    v.get('eta_to_dharamtar') or '',
        'wt_r19':              v.get('wt_r19') or '',
        'at_gull_loaded':      v.get('at_gull_loaded') or '',
        'mbc_eta':             v.get('mbc_eta') or '',
    } for v in vessels_raw]
    return jsonify(vessels)


@bp.route('/api/module/RP01/tide-data')
@login_required
def api_tide_data():
    from_datetime = _parse_dt(request.args.get('from_datetime'))
    to_datetime   = _parse_dt(request.args.get('to_datetime'))
    if not from_datetime or not to_datetime:
        return jsonify([])
    return jsonify(_fetch_tide_data(from_datetime, to_datetime))


@bp.route('/api/module/RP01/shift-details')
@login_required
def get_shift_details():
    conn = get_db()
    cur  = get_cursor(conn)
    cur.execute("SELECT name FROM port_shift_incharge ORDER BY name")
    shift_incharge_list = [r["name"] for r in cur.fetchall()]
    cur.execute("SELECT name FROM port_shift_operators ORDER BY name")
    crane_operator_list = [r["name"] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify({
        "shift_incharge_list": shift_incharge_list,
        "crane_operator_list": crane_operator_list,
    })


@bp.route('/api/module/RP01/shift-wise-discharge')
@login_required
def shift_wise_discharge():
    import traceback
    try:
        return _shift_wise_discharge_inner()
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

def _shift_wise_discharge_inner():
    selected_date = request.args.get('date', '')
    shift = request.args.get('shift', 'ALL')
    if not selected_date:
        return jsonify({'error': 'date is required'}), 400

    conn = get_db()
    cur = get_cursor(conn)
    is_all = shift.upper() == 'ALL'

    # ── 1. JETTY DISCHARGE ───────────────────────────────────────────────────
    if is_all:
        cur.execute("""
            SELECT cargo_name, COALESCE(SUM(quantity), 0) AS qty
            FROM lueu_lines
            WHERE entry_date = %s
              AND quantity > 0
              AND cargo_name IS NOT NULL AND cargo_name != ''
              AND is_deleted IS NOT TRUE
            GROUP BY cargo_name ORDER BY cargo_name
        """, (selected_date,))
    else:
        cur.execute("""
            SELECT cargo_name, COALESCE(SUM(quantity), 0) AS qty
            FROM lueu_lines
            WHERE entry_date = %s AND shift = %s
              AND quantity > 0
              AND cargo_name IS NOT NULL AND cargo_name != ''
              AND is_deleted IS NOT TRUE
            GROUP BY cargo_name ORDER BY cargo_name
        """, (selected_date, shift))
    jetty_rows = [dict(r) for r in cur.fetchall()]

    # ── 2. CLEANING DELAYS ───────────────────────────────────────────────────
    if is_all:
        cur.execute("""
            SELECT source_id, source_type, delay_name, barge_name
            FROM lueu_lines
            WHERE entry_date = %s
              AND is_deleted IS NOT TRUE
              AND delay_name IS NOT NULL AND delay_name != ''
                AND (
                    LOWER(delay_name) LIKE '%%payloader%%'
                    OR LOWER(delay_name) LIKE '%%labor cleaning%%'
                )
        """, (selected_date,))
    else:
        cur.execute("""
            SELECT source_id, source_type, delay_name,barge_name
            FROM lueu_lines
            WHERE entry_date = %s AND shift = %s
              AND is_deleted IS NOT TRUE
              AND delay_name IS NOT NULL AND delay_name != ''
                AND (
                    LOWER(delay_name) LIKE '%%payloader%%'
                    OR LOWER(delay_name) LIKE '%%labor cleaning%%'
                )
        """, (selected_date, shift))

    delay_map = {}
    for row in cur.fetchall():
        key = (row["source_id"], row["source_type"])

        if key not in delay_map:
            delay_map[key] = {
                "payloader": False,
                "labour": False
            }

        delay = (row["delay_name"] or "").strip().lower()

        print("DELAY ROW:", row["source_id"], row["source_type"], delay)

        if "payloader" in delay:
            delay_map[key]["payloader"] = True

        if "labor cleaning" in delay or "labour cleaning" in delay:
            delay_map[key]["labour"] = True

    print("DELAY MAP:", delay_map)


    # ── 3. BARGE DISCHARGE ──
    if is_all:
            cur.execute("""
                WITH actual AS (
                    SELECT
                        TRIM(UPPER(barge_name)) AS barge_key,
                        source_id,
                        SUM(COALESCE(quantity,0)) AS actual_qty
                    FROM lueu_lines
                    WHERE is_deleted IS NOT TRUE AND source_type = 'VCN'
                    AND entry_date = %s
                    GROUP BY 1, 2
                    HAVING SUM(COALESCE(quantity,0)) > 0
                )
                SELECT bl.id, bl.barge_name, bl.trip_number, bl.cargo_name,
                    COALESCE(bl.discharge_quantity, 0) AS bl_qty,
                    COALESCE(a.actual_qty, 0) AS actual_discharge,
                    bl.along_side_berth, bl.commence_discharge_berth,
                    bl.completed_discharge_berth, bl.cast_off_port, h.vcn_id
                FROM ldud_barge_lines bl
                JOIN ldud_header h ON h.id = bl.ldud_id
                LEFT JOIN actual a
                    ON a.barge_key = TRIM(
                        UPPER(
                            CONCAT(
                                bl.barge_name,
                                ' / ',
                                COALESCE(bl.trip_number::text,'1')
                            )
                        )
                    )
                AND a.source_id = h.vcn_id
                WHERE COALESCE(TRIM(bl.barge_name),'') <> ''
                AND COALESCE(a.actual_qty,0) > 0
                ORDER BY bl.barge_name
            """, (selected_date,))
    else:
        cur.execute("""
                WITH actual AS (
                    SELECT
                        TRIM(UPPER(barge_name)) AS barge_key,
                        source_id,
                        SUM(COALESCE(quantity,0)) AS actual_qty
                    FROM lueu_lines
                    WHERE is_deleted IS NOT TRUE AND source_type = 'VCN'
                    AND entry_date = %s AND shift = %s
                    GROUP BY 1, 2
                )
                SELECT bl.id, bl.barge_name,bl.trip_number, bl.cargo_name,
                    COALESCE(bl.discharge_quantity, 0) AS bl_qty,
                    COALESCE(a.actual_qty, 0) AS actual_discharge,
                    bl.along_side_berth, bl.commence_discharge_berth,
                    bl.completed_discharge_berth, bl.cast_off_port, h.vcn_id
                FROM ldud_barge_lines bl
                JOIN ldud_header h ON h.id = bl.ldud_id
                INNER JOIN actual a
                    ON a.barge_key = TRIM(
                        UPPER(
                            CONCAT(
                                bl.barge_name,
                                ' / ',
                                COALESCE(bl.trip_number::text,'1')
                            )
                        )
                    )
                AND a.source_id = h.vcn_id
                WHERE COALESCE(TRIM(bl.barge_name),'') <> ''
                AND COALESCE(a.actual_qty,0) > 0
                ORDER BY bl.barge_name
            """, (selected_date, shift))

    from datetime import date as date_cls
    cutoff = date_cls(2026, 5, 1)
    sel_date_obj = datetime.strptime(selected_date, '%Y-%m-%d').date()

    barge_discharge = []
    for row in cur.fetchall():
        row = dict(row)
        if sel_date_obj < cutoff:
            continue
        if row.get('cast_off_port') and str(row['cast_off_port']).strip():
            status = 'Completed'
        elif row.get('completed_discharge_berth') and str(row['completed_discharge_berth']).strip():
            status = 'Completed'
        elif row.get('commence_discharge_berth') and str(row['commence_discharge_berth']).strip():
            status = 'Under Discharge'
        else:
            status = 'Waiting'

        delays = delay_map.get(
            (row['vcn_id'], 'VCN'),
            {'payloader': False, 'labour': False}
        )
        print(
            "BARGE:",
            row["barge_name"],
            row["vcn_id"],
            delays
        )
        barge_discharge.append({
            'type': 'BARGE',
            'name': row['barge_name'],
            'cargo': row.get('cargo_name') or '',
            'bl_qty': float(row['bl_qty'] or 0),
            'actual_discharge': float(row['actual_discharge'] or 0),
            'status': status,

            'payloader_cl': row['barge_name'] if delays['payloader'] else '',
            'labour_cleaned': row['barge_name'] if delays['labour'] else '',
        })

        # ── 4. MBC DISCHARGE ─────────────────────────────────────────────────────

    if is_all:
            cur.execute("""
                SELECT
                    l.source_id AS id,
                    COALESCE(NULLIF(TRIM(l.barge_name), ''), h.mbc_name) AS mbc_name,
                    COALESCE(l.cargo_name, h.cargo_name) AS cargo_name,
                    COALESCE(h.bl_quantity, 0) AS bl_qty,
                    SUM(COALESCE(l.quantity,0)) AS actual_discharge
                FROM lueu_lines l
                JOIN mbc_header h
                    ON h.id = l.source_id
                WHERE l.source_type = 'MBC'
                AND l.is_deleted IS NOT TRUE
                AND l.entry_date = %s
                GROUP BY
                    l.source_id,
                    COALESCE(NULLIF(TRIM(l.barge_name), ''), h.mbc_name),
                    COALESCE(l.cargo_name, h.cargo_name),
                    h.bl_quantity
                HAVING SUM(COALESCE(l.quantity,0)) > 0
                ORDER BY
                    COALESCE(NULLIF(TRIM(l.barge_name), ''), h.mbc_name)
            """, (selected_date,))
    else:
            cur.execute("""
                SELECT
                    l.source_id AS id,
                    COALESCE(NULLIF(TRIM(l.barge_name), ''), h.mbc_name) AS mbc_name,
                    COALESCE(l.cargo_name, h.cargo_name) AS cargo_name,
                    COALESCE(h.bl_quantity, 0) AS bl_qty,
                    SUM(COALESCE(l.quantity,0)) AS actual_discharge
                FROM lueu_lines l
                JOIN mbc_header h
                    ON h.id = l.source_id
                WHERE l.source_type = 'MBC'
                AND l.is_deleted IS NOT TRUE
                AND l.entry_date = %s
                AND l.shift = %s
                GROUP BY
                    l.source_id,
                    COALESCE(NULLIF(TRIM(l.barge_name), ''), h.mbc_name),
                    COALESCE(l.cargo_name, h.cargo_name),
                    h.bl_quantity
                HAVING SUM(COALESCE(l.quantity,0)) > 0
                ORDER BY
                    COALESCE(NULLIF(TRIM(l.barge_name), ''), h.mbc_name)
            """, (selected_date, shift))

    mbc_discharge = []

    for row in cur.fetchall():
        row = dict(row)

        delays = delay_map.get(
            (row['id'], 'MBC'),
            {'payloader': False, 'labour': False}
        )
        print(
            "MBC:",
            row["mbc_name"],
            row["id"],
            delays
        )

        mbc_discharge.append({
            'type': 'MBC',
            'name': row['mbc_name'],
            'cargo': row.get('cargo_name') or '',
            'bl_qty': float(row['bl_qty'] or 0),
            'actual_discharge': float(row['actual_discharge'] or 0),
            'status': 'Discharging',
            'payloader_cl': row['mbc_name'] if delays['payloader'] else '',
            'labour_cleaned': row['mbc_name'] if delays['labour'] else '',
        })

    cur.close()
    conn.close()

    return jsonify({
        'jetty_discharge': jetty_rows,
        'barge_discharge': barge_discharge,
        'mbc_discharge': mbc_discharge,
    })

@bp.route('/api/module/RP01/shift-report/save', methods=['POST'])
@login_required
def api_shift_report_save():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400

    import json as _json

    conn = get_db()
    cur  = get_cursor(conn)

    try:
        # Separate berth_layout (berths only) and waiting_area from the combined list
        all_layout    = data.get('berth_layout', [])
        berth_layout  = [item for item in all_layout if item.get('berth') != 'WAITING']
        waiting_area  = [item for item in all_layout if item.get('berth') == 'WAITING']

        cur.execute("""
            INSERT INTO barge_position_report
                (report_date, shift, shift_incharge, bpo, crane_operator,
                 berth_layout, waiting_area, wt_r19, mbc_eta,
                 eta_to_dharamtar, on_the_way_gull, shift_plan,
                 notes, movement_logs, updated_at)
            VALUES (%s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, NOW())
            ON CONFLICT (report_date, shift)
            DO UPDATE SET
                shift_incharge  = EXCLUDED.shift_incharge,
                bpo             = EXCLUDED.bpo,
                crane_operator  = EXCLUDED.crane_operator,
                berth_layout    = EXCLUDED.berth_layout,
                waiting_area    = EXCLUDED.waiting_area,
                wt_r19          = EXCLUDED.wt_r19,
                mbc_eta         = EXCLUDED.mbc_eta,
                eta_to_dharamtar= EXCLUDED.eta_to_dharamtar,
                on_the_way_gull = EXCLUDED.on_the_way_gull,
                shift_plan      = EXCLUDED.shift_plan,
                notes           = EXCLUDED.notes,
                movement_logs   = EXCLUDED.movement_logs,
                updated_at      = NOW()
        """, (
            data.get('report_date'),
            data.get('shift'),
            data.get('shift_incharge', ''),
            data.get('bpo', ''),
            data.get('crane_operator', ''),
            _json.dumps(berth_layout),
            _json.dumps(waiting_area),
            _json.dumps(data.get('wt_r19', {})),
            _json.dumps(data.get('mbc_eta', {})),
            _json.dumps(data.get('eta_to_dharamtar', {})),
            _json.dumps(data.get('on_the_way_gull', {})),
            _json.dumps(data.get('shift_plan', {})),
            _json.dumps(data.get('notes', [])),
            _json.dumps(data.get('movement_logs', [])),
        ))
        conn.commit()

    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({'error': str(e)}), 500

    cur.close()
    conn.close()
    return jsonify({'ok': True})


@bp.route('/api/module/RP01/shift-report/load')
@login_required
def api_shift_report_load():
    report_date = request.args.get('date')
    shift       = request.args.get('shift', 'ALL')
    if not report_date:
        return jsonify({'found': False})

    conn = get_db()
    cur  = get_cursor(conn)

    try:
        cur.execute("""
            SELECT * FROM barge_position_report
            WHERE report_date = %s AND shift = %s
        """, (report_date, shift))
        row = cur.fetchone()
    except Exception as e:
        cur.close()
        conn.close()
        return jsonify({'found': False, 'error': str(e)})

    cur.close()
    conn.close()

    if not row:
        return jsonify({'found': False})

    row = dict(row)

    # Merge berth_layout and waiting_area back into one list
    # (frontend sends/receives them combined)
    berth_layout = row.get('berth_layout') or []
    waiting_area = row.get('waiting_area') or []
    combined     = berth_layout + waiting_area

    return jsonify({
        'found':           True,
        'shift_incharge':  row.get('shift_incharge', ''),
        'bpo':             row.get('bpo', ''),
        'crane_operator':  row.get('crane_operator', ''),
        'berth_layout':    combined,
        'wt_r19':          row.get('wt_r19')          or {},
        'mbc_eta':         row.get('mbc_eta')         or {},
        'eta_to_dharamtar':row.get('eta_to_dharamtar')or {},
        'on_the_way_gull': row.get('on_the_way_gull') or {},
        'shift_plan':      row.get('shift_plan')      or {},
        'notes':           row.get('notes')           or [],
        'movement_logs':   row.get('movement_logs')   or [],
        'updated_at':      str(row.get('updated_at', '')),
    })