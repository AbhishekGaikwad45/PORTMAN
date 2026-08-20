from flask import render_template, request, jsonify, session, redirect, url_for
from functools import wraps
from datetime import date, datetime
import json

from .. import bp
from database import get_db, get_cursor


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def _ensure_table():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS saved_pivot_reports (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR(255) NOT NULL,
            description TEXT,
            data_source VARCHAR(100) NOT NULL,
            config      JSONB NOT NULL,
            created_by  INTEGER,
            created_at  TIMESTAMP DEFAULT NOW(),
            updated_at  TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    conn.close()


def _default_dates():
    today = date.today()
    return today.replace(day=1).strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')


def _row_to_dict(row):
    out = {}
    for k, v in row.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
        elif v is None:
            out[k] = ''
        else:
            out[k] = v
    return out


# ── Main page ────────────────────────────────────────────────────────────────

@bp.route('/module/RP01/custom-report/')
@login_required
def custom_report_index():
    _ensure_table()
    return render_template('custom_report/custom_report.html',
                           username=session.get('username'))


# ── Data sources ─────────────────────────────────────────────────────────────

VALID_SOURCES = {'mbc-ops', 'vessel-ops', 'vessel-barge', 'vessel-anchorage', 'lueu-equipment', 'lueu-historical', 'mbc-tat'}

# Maps date_col key → (sql_expression, is_datetime)
# is_datetime=True  → filter uses LEFT(expr::TEXT, 10)
# is_datetime=False → filter uses expr directly
DATE_COL_FILTERS = {
    'mbc-ops': {
        'doc_date':               ("h.doc_date", False),
        'created_date':           ("h.created_date", False),
        'lp_loading_commenced':   ("COALESCE(lp.loading_commenced, elp.loading_commenced)", True),
        'lp_loading_completed':   ("COALESCE(lp.loading_completed, elp.loading_completed)", True),
        'dp_unloading_commenced': ("dp.unloading_commenced", True),
        'dp_unloading_completed': ("dp.unloading_completed", True),
    },
    'vessel-ops': {
        'nor_tendered':       ("h.nor_tendered", True),
        'discharge_date':     ("h.discharge_commenced", True),
        'completion_date':    ("h.discharge_completed", True),
    },
    'vessel-barge': {
        'nor_tendered':          ("h.nor_tendered", True),
        'discharge_commenced':   ("h.discharge_commenced", True),
        'discharge_completed':   ("h.discharge_completed", True),
        'commenced_loading':     ("bl.commenced_loading", True),
        'completed_loading':     ("bl.completed_loading", True),
    },
    'vessel-anchorage': {
        'nor_tendered':        ("h.nor_tendered", True),
        'anchored':            ("a.anchored", True),
        'discharge_started':   ("a.discharge_started", True),
        'discharge_commenced': ("a.discharge_commenced", True),
        'anchor_aweigh':       ("a.anchor_aweigh", True),
    },
    'lueu-equipment': {
        'entry_date': ("l.entry_date", False),
    },
    'lueu-historical': {
        # entry_date is TEXT in lueu_lines but DATE in rp01_historical_lueu;
        # cast to text so NULLIF/BETWEEN works for both UNION legs (ISO dates
        # compare lexically).
        'entry_date': ("entry_date::TEXT", False),
    },
    'mbc-tat': {
        'doc_date': ("h.doc_date", False),
    },
}

# Default date_col key per source (used when none specified)
DATE_COL_DEFAULTS = {
    'mbc-ops':        'doc_date',
    'vessel-ops':     'nor_tendered',
    'vessel-barge':   'nor_tendered',
    'vessel-anchorage': 'nor_tendered',
    'lueu-equipment': 'entry_date',
    'lueu-historical': 'entry_date',
    'mbc-tat':        'doc_date',
}


def _diff_mins(row, col_from, col_to):
    """Compute duration in minutes between two timestamp columns in a row dict."""
    def parse(v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, date):
            return datetime(v.year, v.month, v.day)
        s = str(v).strip()
        if not s:
            return None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass
        return None

    a, b = parse(row.get(col_from)), parse(row.get(col_to))
    if not a or not b:
        return None
    delta = (b - a).total_seconds() / 60
    return round(delta, 1) if delta >= 0 else None


def _build_date_where(source, date_col, from_date, to_date):
    """Return (where_clause_str, params_tuple) for the chosen date column.

    Inputs may be 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' (datetime-local).
    - is_dt=True  (timestamp columns): full TIMESTAMP comparison; T→space.
    - is_dt=False (date columns):      date-only comparison; time part stripped.
    """
    col_map  = DATE_COL_FILTERS.get(source, {})
    default  = DATE_COL_DEFAULTS.get(source, '')
    key      = date_col if date_col in col_map else default
    expr, is_dt = col_map.get(key, col_map.get(default, ("'1900-01-01'", False)))
    if is_dt:
        from_val = (from_date or '').replace('T', ' ') or '1900-01-01 00:00'
        to_val   = (to_date   or '').replace('T', ' ') or '2999-12-31 23:59'
        clause = (f"NULLIF({expr}::TEXT, '') IS NOT NULL"
                  f" AND {expr}::TIMESTAMP BETWEEN %s AND %s")
    else:
        from_val = (from_date or '')[:10] or '1900-01-01'
        to_val   = (to_date   or '')[:10] or '2999-12-31'
        clause = f"NULLIF({expr}, '') IS NOT NULL AND {expr} BETWEEN %s AND %s"
    return clause, (from_val, to_val)


def fetch_source_rows(source, date_col=None, from_date=None, to_date=None, conn=None):
    """Run a Custom Report data source and return plain dict rows.

    Split out of the pivot_data view so other callers (RP01 AI chat) reuse
    these trusted, parameterized queries instead of writing their own SQL.
    Raises ValueError on an unknown source.

    Pass `conn` to run on a specific connection - the AI chat uses a read-only
    role so nothing on that path can write, whatever else goes wrong. The
    caller owns a connection it supplies; only one opened here gets closed.
    """
    if source not in VALID_SOURCES:
        raise ValueError('Unknown data source')

    default_from, default_to = _default_dates()
    from_date = from_date or default_from
    to_date   = to_date   or default_to
    date_col  = date_col  or DATE_COL_DEFAULTS.get(source, '')

    where_clause, where_params = _build_date_where(source, date_col, from_date, to_date)

    owned = conn is None
    conn = conn or get_db()
    cur  = get_cursor(conn)

    try:
        if source == 'mbc-ops':
            cur.execute(f"""
                SELECT
                    h.doc_num                                           AS "Doc No",
                    COALESCE(h.doc_series, '')                          AS "Doc Series",
                    COALESCE(h.mbc_name, '')                            AS "MBC Name",
                    COALESCE(h.operation_type, '')                      AS "Operation Type",
                    COALESCE(h.cargo_type, '')                          AS "Cargo Type",
                    COALESCE(h.cargo_name, '')                          AS "Cargo Name",
                    COALESCE(vc.cargo_category, '')                     AS "Cargo Category",
                    COALESCE(vc.cargo_category_2, '')                   AS "Cargo Category 2",
                    COALESCE(vc.cargo_sub_category, '')                 AS "Cargo Sub Category",
                    COALESCE(vc.cargo_sub_category_2, '')               AS "Cargo Sub Category 2",
                    COALESCE(h.bl_quantity, 0)                          AS "BL Qty",
                    COALESCE(h.quantity_uom, '')                        AS "UOM",
                    COALESCE(h.doc_status, '')                          AS "Status",
                    COALESCE(h.created_by, '')                          AS "Created By",
                    COALESCE(
                        STRING_AGG(DISTINCT cd.customer_name, ', ')
                            FILTER (WHERE cd.customer_name IS NOT NULL),
                    '')                                                 AS "Customer",
                    COALESCE(elp.unloaded_by, '')                       AS "LP Unloaded By (Export)",
                    COALESCE(elp.berth_master, '')                      AS "LP Berth Master (Export)",
                    COALESCE(dp.vessel_unloaded_by, '')                 AS "DP Vessel Unloaded By",
                    COALESCE(dp.vessel_unloading_berth, '')             AS "DP Unloading Berth",
                    COALESCE(dp.discharge_stop_shifting, '')            AS "DP Stop Shifting",
                    COALESCE(dp.discharge_start_shifting, '')           AS "DP Start Shifting",
                    COALESCE(h.doc_date::TEXT, '')                      AS "Doc Date",
                    COALESCE(LEFT(h.doc_date::TEXT, 4), '')             AS "Year",
                    COALESCE(LEFT(h.doc_date::TEXT, 7), '')             AS "Year-Month"
                FROM mbc_header h
                LEFT JOIN mbc_load_port_lines        lp  ON lp.mbc_id  = h.id
                LEFT JOIN mbc_export_load_port_lines elp ON elp.mbc_id = h.id
                LEFT JOIN mbc_discharge_port_lines   dp  ON dp.mbc_id  = h.id
                LEFT JOIN mbc_customer_details       cd  ON cd.mbc_id  = h.id
                LEFT JOIN LATERAL (
                    SELECT cargo_category, cargo_category_2, cargo_sub_category, cargo_sub_category_2
                    FROM vessel_cargo WHERE cargo_name = h.cargo_name LIMIT 1
                ) vc ON TRUE
                WHERE {where_clause}
                GROUP BY h.id, h.doc_date, lp.id, elp.id, dp.id, vc.cargo_category, vc.cargo_category_2, vc.cargo_sub_category, vc.cargo_sub_category_2
                ORDER BY h.id DESC
                LIMIT 100000
            """, where_params)

        elif source == 'vessel-ops':
            cur.execute(f"""
                SELECT
                    h.doc_num                                           AS "Doc No",
                    h.vcn_doc_num                                       AS "VCN No",
                    COALESCE(h.vessel_name, '')                         AS "Vessel",
                    COALESCE(v.operation_type, h.operation_type, '')    AS "Operation Type",
                    COALESCE(v.vessel_agent_name, '')                   AS "Vessel Agent",
                    COALESCE(STRING_AGG(DISTINCT cd.cargo_name, ', '), '') AS "Cargo",
                    COALESCE(ROUND(CAST(SUM(cd.bl_quantity) AS NUMERIC), 0), 0) AS "BL Qty (MT)",
                    CASE
                        WHEN NULLIF(h.discharge_commenced, '') IS NOT NULL
                         AND NULLIF(h.discharge_completed,  '') IS NOT NULL
                        THEN ROUND(CAST(
                            EXTRACT(EPOCH FROM (
                                CAST(h.discharge_completed  AS TIMESTAMP) -
                                CAST(h.discharge_commenced  AS TIMESTAMP)
                            )) / 86400.0 AS NUMERIC
                        ), 2)
                        ELSE NULL
                    END                                                 AS "Actual Days",
                    COALESCE(h.doc_status, '')                          AS "Status",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 10), '')        AS "NOR Date",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 4), '')         AS "Year",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 7), '')         AS "Year-Month"
                FROM ldud_header h
                LEFT JOIN vcn_header v ON v.id = h.vcn_id
                LEFT JOIN vcn_cargo_declaration cd ON cd.vcn_id = h.vcn_id
                WHERE {where_clause}
                GROUP BY h.id, h.doc_num, h.vcn_doc_num, h.vessel_name,
                         v.operation_type, h.operation_type, v.vessel_agent_name,
                         h.nor_tendered, h.discharge_commenced, h.discharge_completed, h.doc_status
                ORDER BY h.nor_tendered DESC
                LIMIT 100000
            """, where_params)

        elif source == 'vessel-barge':
            cur.execute(f"""
                SELECT
                    h.doc_num                                               AS "Doc No",
                    COALESCE(h.vcn_doc_num, '')                            AS "VCN No",
                    COALESCE(h.vessel_name, '')                            AS "Vessel",
                    COALESCE(v.operation_type, h.operation_type, '')       AS "Operation Type",
                    COALESCE(v.vessel_agent_name, '')                      AS "Vessel Agent",
                    COALESCE(h.doc_status, '')                             AS "Status",
                    COALESCE(h.created_by, '')                             AS "Created By",
                    COALESCE(h.initial_draft_survey_quantity::TEXT, '')    AS "Initial Draft Survey Qty",
                    COALESCE(bl.trip_number::TEXT, '')                     AS "Trip No",
                    COALESCE(bl.hold_name, '')                             AS "Hold",
                    COALESCE(bl.barge_name, '')                            AS "Barge",
                    COALESCE(bl.contractor_name, '')                       AS "Contractor",
                    COALESCE(bl.cargo_name, '')                            AS "Cargo",
                    COALESCE(bl.bpt_bfl, '')                               AS "BPT/BFL",
                    COALESCE(bl.discharge_quantity::TEXT, '')              AS "Discharge Qty",
                    COALESCE(bl.crane_loaded_from, '')                     AS "Crane Loaded From",
                    COALESCE(bl.port_crane, '')                            AS "Port Crane",
                    COALESCE(vc.cargo_type, '')                    AS "Cargo Type",
                    COALESCE(vc.cargo_category, '')                AS "Cargo Category",
                    COALESCE(vc.cargo_category_2, '')              AS "Cargo Category 2",
                    COALESCE(vc.cargo_sub_category, '')            AS "Cargo Sub Category",
                    COALESCE(vc.cargo_sub_category_2, '')          AS "Cargo Sub Category 2",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 10), '')   AS "NOR Date",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 4), '')    AS "Year",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 7), '')    AS "Year-Month"
                FROM ldud_header h
                LEFT JOIN vcn_header v ON v.id = h.vcn_id
                LEFT JOIN ldud_barge_lines bl ON bl.ldud_id = h.id
                LEFT JOIN LATERAL (
                    SELECT cargo_type, cargo_category, cargo_category_2, cargo_sub_category, cargo_sub_category_2
                    FROM vessel_cargo WHERE cargo_name = bl.cargo_name LIMIT 1
                ) vc ON TRUE
                WHERE {where_clause}
                ORDER BY h.nor_tendered DESC, h.id, bl.trip_number
                LIMIT 100000
            """, where_params)

        elif source == 'vessel-anchorage':
            cur.execute(f"""
                SELECT
                    h.doc_num                                          AS "Doc No",
                    COALESCE(h.vcn_doc_num, '')                        AS "VCN No",
                    COALESCE(h.vessel_name, '')                        AS "Vessel",
                    COALESCE(v.operation_type, h.operation_type, '')   AS "Operation Type",
                    COALESCE(v.vessel_agent_name, '')                  AS "Vessel Agent",
                    COALESCE(h.doc_status, '')                         AS "Status",
                    COALESCE(h.created_by, '')                         AS "Created By",
                    COALESCE(a.anchorage_name, '')                     AS "Anchorage",
                    COALESCE(a.anchored::TEXT, '')                     AS "Anchored",
                    COALESCE(a.discharge_started::TEXT, '')            AS "Discharge Started",
                    COALESCE(a.discharge_commenced::TEXT, '')          AS "Discharge Commenced",
                    COALESCE(a.anchor_aweigh::TEXT, '')                AS "Anchor Aweigh",
                    COALESCE(a.cargo_quantity, 0)                      AS "Cargo Qty",
                    COALESCE(a.cargo_name, '')                         AS "Cargo",
                    COALESCE(vc.cargo_type, '')                        AS "Cargo Type",
                    COALESCE(vc.cargo_category, '')                    AS "Cargo Category",
                    COALESCE(vc.cargo_category_2, '')                  AS "Cargo Category 2",
                    COALESCE(vc.cargo_sub_category, '')                AS "Cargo Sub Category",
                    COALESCE(vc.cargo_sub_category_2, '')              AS "Cargo Sub Category 2",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 10), '')       AS "NOR Date",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 4), '')        AS "Year",
                    COALESCE(LEFT(h.nor_tendered::TEXT, 7), '')        AS "Year-Month"
                FROM ldud_header h
                LEFT JOIN vcn_header v ON v.id = h.vcn_id
                LEFT JOIN ldud_anchorage a ON a.ldud_id = h.id
                LEFT JOIN LATERAL (
                    SELECT cargo_type, cargo_category, cargo_category_2, cargo_sub_category, cargo_sub_category_2
                    FROM vessel_cargo WHERE cargo_name = a.cargo_name LIMIT 1
                ) vc ON TRUE
                WHERE {where_clause}
                ORDER BY h.nor_tendered DESC, h.id, a.id
                LIMIT 100000
            """, where_params)

        elif source == 'lueu-equipment':
            cur.execute(f"""
                SELECT
                    COALESCE(l.equipment_name, '')      AS "Equipment",
                    COALESCE(l.shift, '')               AS "Shift",
                    COALESCE(l.source_display, '')      AS "VCN / MBC",
                    COALESCE(l.barge_name, '')          AS "Barge / MBC Name",
                    COALESCE(l.cargo_name, '')          AS "Cargo",
                    COALESCE(l.delay_name, '')          AS "Delay",
                    COALESCE(l.system_name, '')         AS "System",
                    COALESCE(l.route_name, '')          AS "Route",
                    COALESCE(l.berth_name, '')          AS "Berth",
                    COALESCE(l.shift_incharge, '')      AS "Shift Incharge",
                    COALESCE(l.operator_name, '')       AS "Operator",
                    COALESCE(l.quantity_uom, '')        AS "UOM",
                    COALESCE(CAST(l.quantity AS TEXT), '') AS "Quantity",
                    COALESCE(l.from_time, '')           AS "_from_time",
                    COALESCE(l.to_time, '')             AS "_to_time",
                    COALESCE(pdt.to_sof, '')               AS "Delay To SOF",
                    COALESCE(pdt.type, '')                  AS "Delay Type",
                    COALESCE(vc.cargo_type, '')             AS "Cargo Type",
                    COALESCE(vc.cargo_category, '')         AS "Cargo Category",
                    COALESCE(vc.cargo_category_2, '')       AS "Cargo Category 2",
                    COALESCE(vc.cargo_sub_category, '')     AS "Cargo Sub Category",
                    COALESCE(vc.cargo_sub_category_2, '')   AS "Cargo Sub Category 2",
                    COALESCE(l.entry_date::TEXT, '')        AS "Date",
                    COALESCE(LEFT(l.entry_date::TEXT, 4), '') AS "Year",
                    COALESCE(LEFT(l.entry_date::TEXT, 7), '') AS "Year-Month"
                FROM lueu_lines l
                LEFT JOIN LATERAL (
                    SELECT to_sof, type
                    FROM port_delay_types WHERE name = l.delay_name LIMIT 1
                ) pdt ON TRUE
                LEFT JOIN LATERAL (
                    SELECT cargo_type, cargo_category, cargo_category_2, cargo_sub_category, cargo_sub_category_2
                    FROM vessel_cargo WHERE cargo_name = l.cargo_name LIMIT 1
                ) vc ON TRUE
                WHERE {where_clause}
                ORDER BY l.id DESC
                LIMIT 100000
            """, where_params)

        elif source == 'lueu-historical':
            cur.execute(f"""
                WITH base AS (
                    SELECT equipment_name, shift, source_display, barge_name, cargo_name,
                           delay_name, system_name, route_name, berth_name, shift_incharge,
                           operator_name, quantity_uom, quantity, from_time, to_time, entry_date
                    FROM lueu_lines
                    WHERE {where_clause}
                    UNION ALL
                    SELECT equipment_name, shift, source_display, barge_name, cargo_name,
                           delay_name, system_name, route_name, berth_name, shift_incharge,
                           operator_name, quantity_uom, quantity, from_time, to_time,
                           entry_date::TEXT AS entry_date
                    FROM rp01_historical_lueu
                    WHERE {where_clause}
                )
                SELECT
                    COALESCE(b.equipment_name, '')      AS "Equipment",
                    COALESCE(b.shift, '')               AS "Shift",
                    COALESCE(b.source_display, '')      AS "VCN / MBC",
                    COALESCE(b.barge_name, '')          AS "Barge / MBC Name",
                    COALESCE(b.cargo_name, '')          AS "Cargo",
                    COALESCE(b.delay_name, '')          AS "Delay",
                    COALESCE(b.system_name, '')         AS "System",
                    COALESCE(b.route_name, '')          AS "Route",
                    COALESCE(b.berth_name, '')          AS "Berth",
                    COALESCE(b.shift_incharge, '')      AS "Shift Incharge",
                    COALESCE(b.operator_name, '')       AS "Operator",
                    COALESCE(b.quantity_uom, '')        AS "UOM",
                    COALESCE(CAST(b.quantity AS TEXT), '') AS "Quantity",
                    COALESCE(b.from_time, '')           AS "_from_time",
                    COALESCE(b.to_time, '')             AS "_to_time",
                    COALESCE(pdt.to_sof, '')               AS "Delay To SOF",
                    COALESCE(pdt.type, '')                  AS "Delay Type",
                    COALESCE(vc.cargo_type, '')             AS "Cargo Type",
                    COALESCE(vc.cargo_category, '')         AS "Cargo Category",
                    COALESCE(vc.cargo_category_2, '')       AS "Cargo Category 2",
                    COALESCE(vc.cargo_sub_category, '')     AS "Cargo Sub Category",
                    COALESCE(vc.cargo_sub_category_2, '')   AS "Cargo Sub Category 2",
                    COALESCE(b.entry_date::TEXT, '')        AS "Date",
                    COALESCE(LEFT(b.entry_date::TEXT, 4), '') AS "Year",
                    COALESCE(LEFT(b.entry_date::TEXT, 7), '') AS "Year-Month"
                FROM base b
                LEFT JOIN LATERAL (
                    SELECT to_sof, type
                    FROM port_delay_types WHERE name = b.delay_name LIMIT 1
                ) pdt ON TRUE
                LEFT JOIN LATERAL (
                    SELECT cargo_type, cargo_category, cargo_category_2, cargo_sub_category, cargo_sub_category_2
                    FROM vessel_cargo WHERE cargo_name = b.cargo_name LIMIT 1
                ) vc ON TRUE
                ORDER BY b.entry_date DESC
                LIMIT 100000
            """, where_params + where_params)

        elif source == 'mbc-tat':
            cur.execute(f"""
                SELECT
                    h.doc_num                                           AS doc_num,
                    COALESCE(h.mbc_name, '')                           AS mbc_name,
                    COALESCE(h.operation_type, '')                     AS operation_type,
                    COALESCE(h.cargo_name, '')                         AS cargo_name,
                    COALESCE(CAST(h.bl_quantity AS TEXT), '')          AS bl_quantity,
                    COALESCE(h.doc_status, '')                         AS doc_status,
                    COALESCE(h.created_by, '')                         AS created_by,
                    COALESCE(vc.cargo_type, '')               AS cargo_type,
                    COALESCE(vc.cargo_category, '')           AS cargo_category,
                    COALESCE(vc.cargo_category_2, '')         AS cargo_category_2,
                    COALESCE(vc.cargo_sub_category, '')       AS cargo_sub_category,
                    COALESCE(vc.cargo_sub_category_2, '')     AS cargo_sub_category_2,
                    COALESCE(h.doc_date::TEXT, '')            AS doc_date,
                    lp.arrived_load_port,    lp.loading_commenced,   lp.loading_completed,
                    lp.cast_off_load_port,
                    dp.arrival_gull_island,  dp.departure_gull_island, dp.vessel_arrival_port,
                    dp.unloading_commenced,  dp.unloading_completed,
                    dp.vessel_cast_off,      dp.sailed_out_load_port
                FROM mbc_header h
                LEFT JOIN mbc_load_port_lines      lp ON lp.mbc_id = h.id
                LEFT JOIN mbc_discharge_port_lines dp ON dp.mbc_id = h.id
                LEFT JOIN LATERAL (
                    SELECT cargo_type, cargo_category, cargo_category_2, cargo_sub_category, cargo_sub_category_2
                    FROM vessel_cargo WHERE cargo_name = h.cargo_name LIMIT 1
                ) vc ON TRUE
                WHERE {where_clause}
                ORDER BY h.doc_date ASC, h.id ASC
                LIMIT 100000
            """, where_params)

        rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        if owned:
            conn.close()

    # Post-process lueu sources: compute Diff Hrs from from_time / to_time (HH:MM)
    if source in ('lueu-equipment', 'lueu-historical'):
        def _calc_diff_hrs(from_t, to_t):
            try:
                fh, fm = int(from_t[:2]), int(from_t[3:5])
                th, tm = int(to_t[:2]),   int(to_t[3:5])
                from_mins = fh * 60 + fm
                to_mins   = th * 60 + tm
                diff = to_mins - from_mins if to_mins > from_mins else 1440 - from_mins + to_mins
                return round(diff / 60, 2)
            except Exception:
                return None

        for r in rows:
            r['From Time'] = r.pop('_from_time', '')
            r['To Time']   = r.pop('_to_time', '')
            r['Diff Hrs']  = _calc_diff_hrs(r['From Time'], r['To Time'])

    # Post-process mbc-tat: replace raw timestamps with computed duration columns
    if source == 'mbc-tat':
        processed = []
        for r in rows:
            processed.append({
                'Doc No':                        r.get('doc_num', ''),
                'MBC Name':                      r.get('mbc_name', ''),
                'Operation Type':                r.get('operation_type', ''),
                'Cargo':                         r.get('cargo_name', ''),
                'BL Quantity':                   r.get('bl_quantity', ''),
                'Status':                        r.get('doc_status', ''),
                'Created By':                    r.get('created_by', ''),
                'Doc Date':             r.get('doc_date', ''),
                'Year':                 r.get('doc_date', '')[:4]  if r.get('doc_date') else '',
                'Year-Month':           r.get('doc_date', '')[:7]  if r.get('doc_date') else '',
                'Cargo Type':           r.get('cargo_type', ''),
                'Cargo Category':       r.get('cargo_category', ''),
                'Cargo Category 2':     r.get('cargo_category_2', ''),
                'Cargo Sub Category':   r.get('cargo_sub_category', ''),
                'Cargo Sub Category 2': r.get('cargo_sub_category_2', ''),
                'Preberthing (min)':             _diff_mins(r, 'arrived_load_port',     'loading_commenced'),
                'Loading Time (min)':            _diff_mins(r, 'loading_commenced',      'loading_completed'),
                'Wait After Load (min)':         _diff_mins(r, 'loading_completed',      'cast_off_load_port'),
                'Total at Jaigad (min)':         _diff_mins(r, 'arrived_load_port',     'cast_off_load_port'),
                'Transit Jaigad-Gull (min)':     _diff_mins(r, 'cast_off_load_port',    'arrival_gull_island'),
                'Gull Waiting (min)':            _diff_mins(r, 'arrival_gull_island',   'departure_gull_island'),
                'Gull-Dharamtar (min)':          _diff_mins(r, 'departure_gull_island', 'vessel_arrival_port'),
                'Jaigad-Dharamtar (min)':        _diff_mins(r, 'cast_off_load_port',    'vessel_arrival_port'),
                'Preberthing Dharamtar (min)':   _diff_mins(r, 'vessel_arrival_port',   'unloading_commenced'),
                'Unloading Time (min)':          _diff_mins(r, 'unloading_commenced',   'unloading_completed'),
                'Wait After Unload (min)':       _diff_mins(r, 'unloading_completed',   'vessel_cast_off'),
                'Total at Dharamtar (min)':      _diff_mins(r, 'vessel_arrival_port',   'vessel_cast_off'),
                'Dharamtar-Jaigad (min)':        _diff_mins(r, 'vessel_cast_off',       'sailed_out_load_port'),
                'TAT (min)':                     _diff_mins(r, 'arrived_load_port',     'sailed_out_load_port'),
            })
        rows = processed

    return rows


@bp.route('/api/module/RP01/pivot/data/<source>')
@login_required
def pivot_data(source):
    try:
        rows = fetch_source_rows(
            source,
            date_col=request.args.get('date_col'),
            from_date=request.args.get('from_date'),
            to_date=request.args.get('to_date'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(rows)


# ── Saved reports CRUD ───────────────────────────────────────────────────────

@bp.route('/api/module/RP01/pivot/saved-reports', methods=['GET'])
@login_required
def saved_reports_list():
    _ensure_table()
    conn = get_db()
    cur  = get_cursor(conn)
    cur.execute("""
        SELECT id, name, description, data_source, config, created_at
        FROM saved_pivot_reports
        ORDER BY updated_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get('created_at'), (date, datetime)):
            d['created_at'] = d['created_at'].isoformat()
        result.append(d)
    return jsonify(result)


@bp.route('/api/module/RP01/pivot/saved-reports', methods=['POST'])
@login_required
def saved_reports_create():
    _ensure_table()
    body = request.get_json(force=True) or {}
    name        = (body.get('name') or '').strip()
    description = (body.get('description') or '').strip()
    data_source = (body.get('data_source') or '').strip()
    config      = body.get('config', {})

    if not name or data_source not in VALID_SOURCES:
        return jsonify({'error': 'name and valid data_source are required'}), 400

    conn = get_db()
    cur  = get_cursor(conn)
    cur.execute("""
        INSERT INTO saved_pivot_reports (name, description, data_source, config, created_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
    """, (name, description, data_source, json.dumps(config), session.get('user_id')))
    new_id = cur.fetchone()['id']
    conn.commit()
    conn.close()
    return jsonify({'id': new_id, 'name': name}), 201


@bp.route('/api/module/RP01/pivot/saved-reports/<int:report_id>', methods=['PUT'])
@login_required
def saved_reports_update(report_id):
    body = request.get_json(force=True) or {}
    name        = (body.get('name') or '').strip()
    description = (body.get('description') or '').strip()
    config      = body.get('config', {})

    if not name:
        return jsonify({'error': 'name is required'}), 400

    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE saved_pivot_reports
        SET name = %s, description = %s, config = %s, updated_at = NOW()
        WHERE id = %s
    """, (name, description, json.dumps(config), report_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@bp.route('/api/module/RP01/pivot/saved-reports/<int:report_id>', methods=['DELETE'])
@login_required
def saved_reports_delete(report_id):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM saved_pivot_reports WHERE id = %s", (report_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})
