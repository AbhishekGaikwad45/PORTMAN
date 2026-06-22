from flask import (
    render_template,
    request,
    session,
    redirect,
    url_for,
    jsonify
)

from functools import wraps


from .. import bp
from database import get_db, get_cursor
from datetime import datetime, timedelta, date

# =========================================================
# LOGIN REQUIRED
# =========================================================

def login_required(f):

    @wraps(f)
    def decorated(*args, **kwargs):

        if 'user_id' not in session:
            return redirect(url_for('login'))

        return f(*args, **kwargs)

    return decorated


# =========================================================
# PAGE
# =========================================================

@bp.route('/module/RP01/report_24_hours/')
@login_required
def report_24_hours_page():

    return render_template(
        'report_24_hours/report_24_hours.html',
        username=session.get('username')
    )


# =========================================================
# API
# =========================================================

@bp.route('/module/RP01/api/report_24_hours', methods=['GET'])
@login_required
def get_24_hours_report():

    conn = None
    cur = None

    try:

        print("\n========== 24 HOURS REPORT API ==========")

        selected_date = request.args.get('report_date')

        print("SELECTED DATE:", selected_date)

        if not selected_date:

            return jsonify({
                'success': False,
                'message': 'report_date is required'
            }), 400



        # =================================================
        # FETCH PREVIOUS DATE
        # =================================================

        selected_date = request.args.get('report_date')

        print("SELECTED DATE:", selected_date)

        if not selected_date:

            return jsonify({
                'success': False,
                'message': 'report_date is required'
            }), 400

        # User selects 15-Jun -> fetch 14-Jun data
        fetch_date = (
            datetime.strptime(
                selected_date,
                '%Y-%m-%d'
            ) - timedelta(days=1)
        )

        print("REPORT DATE:", fetch_date.date())

        # =================================================
        # DATABASE CONNECTION
        # =================================================

        conn = get_db()

        print("DB CONNECTION SUCCESS")

        cur = get_cursor(conn)

        print("CURSOR CREATED")

        # =================================================
        # MV DISCHARGE
        # =================================================

        try:

            print("\n--- FETCHING MV DISCHARGE ---")

            selected_dt = datetime.strptime(
                selected_date,
                '%Y-%m-%d'
            )

            # Reporting Window
            window_start = (
                selected_dt - timedelta(days=1)
            ).replace(
                hour=8,
                minute=0,
                second=0,
                microsecond=0
            )

            window_end = selected_dt.replace(
                hour=8,
                minute=0,
                second=0,
                microsecond=0
            )

            print("WINDOW START:", window_start)
            print("WINDOW END  :", window_end)

            # Since start_time only contains dates,
            # use previous day's date for quantity calculation
            target_date = window_start.date()

            print("TARGET DATE:", target_date)

            mv_disch = 0
            mv_total_qty = 0
            mv_total_days = 0
            mv_discharge_list = []

            # ------------------------------------
            # 24 HRS DISCHARGE
            # ------------------------------------

            cur.execute("""
                SELECT COALESCE(SUM(quantity), 0) AS qty
                FROM ldud_vessel_operations
                WHERE TO_DATE(start_time, 'YYYY-MM-DD') = %s
            """, (target_date,))

            row = cur.fetchone()

            mv_disch = float(row['qty'] or 0)

            print("24 HRS MV DISCHARGE:", mv_disch)

            # ------------------------------------
            # DISCHARGE TILL DATE
            # ------------------------------------

            cur.execute("""
                SELECT COALESCE(SUM(quantity), 0) AS qty
                FROM ldud_vessel_operations
                WHERE TO_DATE(start_time, 'YYYY-MM-DD') <= %s
            """, (target_date,))

            row = cur.fetchone()

            mv_total_qty = float(row['qty'] or 0)

            print("MV DISCHARGE TILL DATE:", mv_total_qty)

        except Exception as e:

            print("MV DISCHARGE ERROR:", str(e))

            mv_disch = 0
            mv_total_qty = 0
            mv_total_days = 0
            mv_discharge_list = []

        # =================================================
        # MV WAITING
        # =================================================

        try:

            print("\n--- FETCHING MV WAITING ---")

            selected_dt = datetime.strptime(
                selected_date,
                '%Y-%m-%d'
            )

            window_start = (
                selected_dt - timedelta(days=1)
            ).replace(
                hour=8,
                minute=0,
                second=0,
                microsecond=0
            )

            window_end = selected_dt.replace(
                hour=8,
                minute=0,
                second=0,
                microsecond=0
            )

            print("WINDOW START:", window_start)
            print("WINDOW END:", window_end)

            cur.execute("""
                SELECT
                    COALESCE(h.vcn_doc_num, '') || ' / ' ||
                    COALESCE(h.vessel_name, '') AS vessel_name

                FROM ldud_header h

                WHERE h.nor_accepted IS NOT NULL
                AND h.discharge_commenced IS NULL

                AND TO_TIMESTAMP(
                        h.nor_accepted,
                        'YYYY-MM-DD"T"HH24:MI'
                    ) >= %s

                AND TO_TIMESTAMP(
                        h.nor_accepted,
                        'YYYY-MM-DD"T"HH24:MI'
                    ) < %s

                AND NOT EXISTS (
                    SELECT 1
                    FROM ldud_anchorage a
                    WHERE a.ldud_id = h.id
                    AND a.discharge_started IS NOT NULL
                )

                ORDER BY vessel_name

            """, (window_start, window_end))

            mv_waiting_rows = cur.fetchall()

            if mv_waiting_rows:

                mv_waiting_list = [
                    {
                        'vessel_name': str(row['vessel_name'])
                    }
                    for row in mv_waiting_rows
                ]

                mv_waiting_count = len(mv_waiting_list)

            else:

                mv_waiting_list = []
                mv_waiting_count = 0

            print("MV WAITING COUNT:", mv_waiting_count)
            print("MV WAITING LIST:", mv_waiting_list)

        except Exception as e:

            print("MV WAITING ERROR:", str(e))

            conn.rollback()

            mv_waiting_list = []
            mv_waiting_count = 0


        # =================================================
        # MBC WAITING
        # =================================================

        try:

            print("\n--- FETCHING MBC WAITING ---")

            cur.execute("""
                SELECT
                    h.mbc_name,
                    h.cargo_name
                FROM mbc_discharge_port_lines d
                INNER JOIN mbc_header h
                    ON h.id = d.mbc_id
                WHERE d.arrival_gull_island IS NOT NULL
                AND d.vessel_arrival_port IS NULL
                ORDER BY h.mbc_name
            """)

            mbc_waiting_rows = [
                {
                    'mbc_name': row['mbc_name'],
                    'cargo_name': row['cargo_name']
                }
                for row in cur.fetchall()
            ]

            if not mbc_waiting_rows:
                mbc_waiting_rows = [
                    {
                        'mbc_name': 'Nil',
                        'cargo_name': ''
                    }
                ]

            print("MBC WAITING COUNT:", len(mbc_waiting_rows))
            print("MBC WAITING:", mbc_waiting_rows)

        except Exception as e:

            print("MBC WAITING ERROR:", str(e))
            conn.rollback()

            mbc_waiting_rows = [
                {
                    'mbc_name': 'Nil',
                    'cargo_name': ''
                }
            ]

        # =================================================
        # MV DISCHARGING / COMPLETED IN LAST 24 HRS
        # =================================================

        mv_discharge_list = []

        try:

            selected_dt = datetime.strptime(
                selected_date,
                '%Y-%m-%d'
            )

            window_end = selected_dt.replace(
                hour=8,
                minute=0,
                second=0,
                microsecond=0
            )

            window_start = window_end - timedelta(hours=24)

            print("MV WINDOW START:", window_start)
            print("MV WINDOW END  :", window_end)

            cur.execute("""
                SELECT DISTINCT

                    h.id,
                    h.vcn_id,
                    h.vessel_name,

                    first_anchor.discharge_started
                        AS discharge_commenced,

                    last_anchor.discharge_completed
                        AS discharge_completed

                FROM ldud_header h

                LEFT JOIN LATERAL (
                    SELECT
                        MIN(a1.discharge_started)
                            AS discharge_started
                    FROM ldud_anchorage a1
                    WHERE a1.ldud_id = h.id
                    AND a1.discharge_started IS NOT NULL
                ) first_anchor ON TRUE

                LEFT JOIN LATERAL (
                    SELECT
                        CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM ldud_anchorage x
                                WHERE x.ldud_id = h.id
                                AND x.discharge_started IS NOT NULL
                                AND x.discharge_commenced IS NULL
                            )
                            THEN NULL
                            ELSE MAX(a2.discharge_commenced)
                        END AS discharge_completed
                    FROM ldud_anchorage a2
                    WHERE a2.ldud_id = h.id
                ) last_anchor ON TRUE

                WHERE

                    first_anchor.discharge_started IS NOT NULL

                    AND first_anchor.discharge_started < %s

                    AND (
                        last_anchor.discharge_completed IS NULL
                        OR last_anchor.discharge_completed >= %s
                    )

                ORDER BY
                    first_anchor.discharge_started ASC,
                    h.vessel_name

            """, (
                window_end,
                window_start
            ))

            rows = cur.fetchall()

            print("MV ROWS FOUND:", len(rows))

            # ------------------------------------
            # SAME LOGIC AS ops_24h / ops_till
            # ------------------------------------

            ops_24h = {}
            ops_till = {}

            ldud_ids = [r['id'] for r in rows]

            prev_date = selected_dt.date() - timedelta(days=1)

            if ldud_ids:

                cur.execute("""
                    SELECT
                        ldud_id,
                        COALESCE(SUM(quantity), 0) AS qty
                    FROM ldud_vessel_operations
                    WHERE ldud_id = ANY(%s)
                    AND TO_DATE(start_time, 'YYYY-MM-DD') = %s
                    GROUP BY ldud_id
                """, (ldud_ids, prev_date))

                for r in cur.fetchall():

                    ops_24h[r['ldud_id']] = float(
                        r['qty'] or 0
                    )

                cur.execute("""
                    SELECT
                        ldud_id,
                        COALESCE(SUM(quantity), 0) AS qty
                    FROM ldud_vessel_operations
                    WHERE ldud_id = ANY(%s)
                    AND TO_DATE(start_time, 'YYYY-MM-DD') <= %s
                    GROUP BY ldud_id
                """, (ldud_ids, prev_date))

                for r in cur.fetchall():

                    ops_till[r['ldud_id']] = float(
                        r['qty'] or 0
                    )

            # ------------------------------------
            # PROCESS VESSELS
            # ------------------------------------

            for row in rows:

                ldud_id = row['id']
                vcn_id = row['vcn_id']

                cargo_name = ''
                bl_qty = 0

                if vcn_id:

                    cur.execute("""
                        SELECT
                            STRING_AGG(
                                DISTINCT cargo_name,
                                ', '
                            ) AS cargo_names,

                            COALESCE(
                                SUM(bl_quantity),
                                0
                            ) AS total_bl

                        FROM vcn_cargo_declaration

                        WHERE vcn_id = %s

                    """, (vcn_id,))

                    cargo_row = cur.fetchone()

                    if cargo_row:

                        cargo_name = (
                            cargo_row['cargo_names']
                            or ''
                        )

                        bl_qty = float(
                            cargo_row['total_bl']
                            or 0
                        )

                # ------------------------------------
                # SAME AS ops_24h
                # ------------------------------------

                discharge_24hrs = ops_24h.get(
                    ldud_id,
                    0
                )

                # ------------------------------------
                # SAME AS ops_till
                # ------------------------------------

                discharge_qty = ops_till.get(
                    ldud_id,
                    0
                )

                balance_qty = max(
                    bl_qty - discharge_qty,
                    0
                )

                # ------------------------------------
                # PREVIOUS DAY DELAYS FOR SAME VESSEL
                # ------------------------------------

                delay_text = ''
                discharge_start_date = ''
                discharge_end_date = ''

                delay_window_start = (
                    selected_dt - timedelta(days=1)
                ).replace(
                    hour=8,
                    minute=0,
                    second=0,
                    microsecond=0
                )

                delay_window_end = selected_dt.replace(
                    hour=8,
                    minute=0,
                    second=0,
                    microsecond=0
                )

                # ------------------------------------
                # FETCH DISCHARGE START / END
                # ------------------------------------

                cur.execute("""
                    SELECT
                        MIN(discharge_started) AS discharge_start,
                        CASE
                            WHEN MAX(discharge_commenced) >= MIN(discharge_started)
                            THEN MAX(discharge_commenced)
                            ELSE NULL
                        END AS discharge_end
                    FROM ldud_anchorage
                    WHERE ldud_id = %s
                    AND discharge_started IS NOT NULL
                    AND (
                        discharge_commenced IS NULL
                        OR discharge_commenced >= discharge_started
                    )
                """, (ldud_id,))

                anchorage_row = cur.fetchone()

                # ------------------------------------
                # DAYS CALCULATION
                # ------------------------------------

                days_factor = 1.00

                raw_start = anchorage_row['discharge_start']
                raw_end = anchorage_row['discharge_end']

                # Vessel completed within report window
                if raw_end and window_start <= raw_end < window_end:

                    completion_hrs = (
                        raw_end - window_start
                    ).total_seconds() / 3600

                    days_factor = completion_hrs / 24

                # Vessel started within report window
                elif raw_start and window_start <= raw_start < window_end:

                    lost_hrs = (
                        raw_start - window_start
                    ).total_seconds() / 3600

                    days_factor = (
                        24 - lost_hrs
                    ) / 24

                # No start/end shown on UI
                else:

                    days_factor = 1.00

                days_factor = round(
                    days_factor,
                    2
                )

                print(
                    "VESSEL:",
                    row['vessel_name'],
                    "DAYS FACTOR:",
                    f"{days_factor:.2f}"
                )

                # ------------------------------------
                # ADD TO TOTAL DAYS
                # ------------------------------------

                mv_total_days = round(
                    mv_total_days + days_factor,
                    2
                )

                print(
                    "TOTAL DAYS SO FAR:",
                    f"{mv_total_days:.2f}"
                )

                if anchorage_row:

                    raw_start = anchorage_row['discharge_start']
                    raw_end = anchorage_row['discharge_end']

                    # Only show if date falls within the report window
                    if raw_start:
                        if delay_window_start <= raw_start < delay_window_end:
                            discharge_start_date = raw_start.strftime(
                                '%d-%b-%Y %H:%M'
                            )

                    if raw_end:
                        if delay_window_start <= raw_end < delay_window_end:
                            discharge_end_date = raw_end.strftime(
                                '%d-%b-%Y %H:%M'
                            )

                print(
                    "ANCHORAGE ROW:",
                    anchorage_row,
                    "LDUD ID:",
                    ldud_id
                )

                print(
                    "DISCHARGE START DATE:",
                    discharge_start_date,
                    "DISCHARGE END DATE:",
                    discharge_end_date
                )
                # ------------------------------------
                # DELAY WINDOW (08:00 AM TO 08:00 AM)
                # ------------------------------------

                delay_window_start = (
                    selected_dt - timedelta(days=1)
                ).replace(
                    hour=8,
                    minute=0,
                    second=0,
                    microsecond=0
                )

                delay_window_end = selected_dt.replace(
                    hour=8,
                    minute=0,
                    second=0,
                    microsecond=0
                )

                print(
                    f"\nDELAY WINDOW: "
                    f"{delay_window_start} TO {delay_window_end}"
                )

                # ------------------------------------
                # FETCH DELAYS WITHIN REPORT WINDOW
                # ------------------------------------

                cur.execute("""
                    SELECT
                        delay_name,
                        crane_number,
                        COALESCE(total_time_hrs, 0) AS total_hrs

                    FROM ldud_delays

                    WHERE ldud_id = %s

                    AND TO_TIMESTAMP(
                        start_datetime,
                        'YYYY-MM-DD"T"HH24:MI'
                    ) >= %s

                    AND TO_TIMESTAMP(
                        start_datetime,
                        'YYYY-MM-DD"T"HH24:MI'
                    ) < %s

                    ORDER BY delay_name

                """, (
                    ldud_id,
                    delay_window_start,
                    delay_window_end
                ))

                delay_rows = cur.fetchall()

                print("\n========== RAW DELAY ROWS ==========")
                print(f"VESSEL: {row['vessel_name']} | LDUD: {ldud_id}")
                print(f"WINDOW: {delay_window_start} TO {delay_window_end}")
                print(f"Total delay rows: {len(delay_rows)}")

                excluded_delays = {
                    'crane idle due to hold completion',
                    'barge approaching'
                }

                delay_totals = {}

                for d in delay_rows:

                    delay_name_mapping = {
                        'Different Type of Cargo': 'DTC',
                        'Different Types of Cargo': 'DTC',
                        'Different Type Cargo': 'DTC',

                        'Want of Barge': 'WOB',

                        'Bad Weather / Heavy Rain': 'Bad Weather',
                        'Bad Weather/ Rain': 'Bad Weather',

                        'VTMS Permission': 'VTMS',

                        'Cement Delay': 'CD',
                        'Cement Loading Delay': 'CD',
                        'Cement Cargo Delay': 'CD'
                    }

                    delay_name = (
                        d['delay_name']
                        or ''
                    ).strip()

                    delay_name = delay_name_mapping.get(delay_name, delay_name)

                    if delay_name.lower() in excluded_delays:

                        print(
                            "SKIPPING DELAY:",
                            delay_name
                        )

                        continue

                    total_hrs = float(
                        d['total_hrs']
                        or 0
                    )

                    crane_text = (
                        d['crane_number']
                        or ''
                    )

                    cranes = set()

                    for part in crane_text.replace(',', ' ').split():

                        part = part.strip()

                        if part:
                            cranes.add(part)

                    total_cranes = len(cranes)

                    if total_cranes <= 0:
                        total_cranes = 1

                    # ------------------------------------
                    # CRANE-WISE DELAY CALCULATION
                    # ------------------------------------

                    adjusted_hrs = (
                        total_hrs * total_cranes
                    ) / 4

                    adjusted_hrs = round(
                        adjusted_hrs,
                        2
                    )

                    delay_totals.setdefault(
                        delay_name,
                        0
                    )

                    delay_totals[
                        delay_name
                    ] += adjusted_hrs

                    print(
                        "DELAY:",
                        delay_name,
                        "| TOTAL HRS:",
                        total_hrs,
                        "| TOTAL CRANES:",
                        total_cranes,
                        "| ADJUSTED HRS:",
                        adjusted_hrs,
                        "| CRANES:",
                        sorted(cranes)
                    )

                delay_parts = []

                for delay_name, total_delay_hrs in delay_totals.items():

                    total_delay_hrs = round(
                        total_delay_hrs,
                        2
                    )

                    print(
                        "FINAL DELAY:",
                        delay_name,
                        "| TOTAL ADJUSTED HRS:",
                        total_delay_hrs
                    )

                    delay_parts.append(
                        f"{delay_name} - {total_delay_hrs} Hrs"
                    )

                print("=====================================\n")

                delay_text = ", ".join(
                    delay_parts
                )

                print(
                    "VESSEL:",
                    row['vessel_name'],
                    "LDUD:",
                    ldud_id,
                    "WINDOW:",
                    delay_window_start,
                    "TO",
                    delay_window_end,
                    "DELAYS:",
                    delay_text
                )
                print(
                    "APPENDING MV:",
                    row['vessel_name']
                )

                mv_discharge_list.append({

                    'vessel_name':
                        row['vessel_name'],

                    'cargo_name':
                        cargo_name,

                    'discharge_24hrs':
                        round(
                            discharge_24hrs,
                            2
                        ),

                    'balance_qty':
                        round(
                            balance_qty,
                            2
                        ),

                    'discharge_start_date':
                        discharge_start_date,

                    'discharge_end_date':
                        discharge_end_date,

                    'delay_name':
                        delay_text

                })

                print(
                    "MV DISCHARGE LIST COUNT:",
                    len(
                        mv_discharge_list
                    )
                )

                print(
                    "MV DISCHARGE LIST:",
                    mv_discharge_list
                )

        except Exception as e:

            print(
                "MV DISCHARGE LIST ERROR:",
                str(e)
            )

            conn.rollback()

            mv_discharge_list = []

        # =================================================
        # MBC DISCHARGING LAST 24 HRS
        # =================================================

        try:

            print("\n--- FETCHING MBC DISCH TOTAL ---")

            target_date = fetch_date.date()

            print("TARGET DATE:", target_date)

            cur.execute("""
                SELECT
                    ROUND(
                        COALESCE(
                            SUM(COALESCE(quantity, 0))::numeric,
                            0
                        ),
                        2
                    ) AS mbc_total_mt
                FROM lueu_lines
                WHERE source_type = 'MBC'
                AND TO_DATE(
                        entry_date,
                        'YYYY-MM-DD'
                    ) = %s
            """, (target_date,))

            mbc_disch_row = cur.fetchone()

            mbc_disch_total = float(
                mbc_disch_row['mbc_total_mt'] or 0
            ) if mbc_disch_row else 0

            print("MBC DISCH TOTAL:", mbc_disch_total)

        except Exception as e:

            print("MBC DISCH ERROR:", str(e))

            conn.rollback()

            mbc_disch_total = 0

        # =================================================
        # BARGE SUMMARY
        # =================================================

        total_barges = 0
        cement_barges = 0
        steel_barges = 0
        barges_count = 0

        try:
            selected_dt = datetime.strptime(selected_date, '%Y-%m-%d')

            window_end = datetime(
                selected_dt.year,
                selected_dt.month,
                selected_dt.day,
                8, 0, 0
            )
            window_start = window_end - timedelta(hours=24)

            ws_str = window_start.strftime('%Y-%m-%d %H:%M:%S')
            we_str = window_end.strftime('%Y-%m-%d %H:%M:%S')

            # Step 1: Fetch active ldud_ids in the window
            cur.execute("""
                SELECT DISTINCT h.id AS ldud_id

                FROM ldud_header h

                LEFT JOIN LATERAL (
                    SELECT MIN(a1.discharge_started) AS discharge_started
                    FROM ldud_anchorage a1
                    WHERE a1.ldud_id = h.id
                    AND a1.discharge_started IS NOT NULL
                ) first_anchor ON TRUE

                LEFT JOIN LATERAL (
                    SELECT
                        CASE
                            WHEN EXISTS (
                                SELECT 1 FROM ldud_anchorage x
                                WHERE x.ldud_id = h.id
                                AND x.discharge_started IS NOT NULL
                                AND x.discharge_commenced IS NULL
                            )
                            THEN NULL
                            ELSE MAX(a2.discharge_commenced)
                        END AS discharge_completed
                    FROM ldud_anchorage a2
                    WHERE a2.ldud_id = h.id
                ) last_anchor ON TRUE

                WHERE
                    first_anchor.discharge_started IS NOT NULL
                    AND first_anchor.discharge_started < %s
                    AND (
                        last_anchor.discharge_completed IS NULL
                        OR last_anchor.discharge_completed >= %s
                        OR EXISTS (
                            SELECT 1 FROM ldud_barge_lines b
                            WHERE b.ldud_id = h.id
                            AND (
                                b.completed_discharge_berth IS NULL
                                OR b.cast_off_berth IS NULL
                            )
                        )
                    )
            """, (we_str, ws_str))

            ldud_ids = [r['ldud_id'] for r in cur.fetchall()]

            print(f"\nActive ldud_ids: {ldud_ids}")

            if not ldud_ids:
                print("No active vessels found for this window.")
            else:
                # Step 2: Fetch barges for those ldud_ids
                cur.execute("""
                    SELECT
                        b.ldud_id,
                        TRIM(b.barge_name) AS barge_name,

                        b.along_side_vessel,
                        b.commenced_loading,
                        b.completed_loading,
                        b.cast_off_mv,

                        b.along_side_berth,
                        b.commence_discharge_berth,
                        b.completed_discharge_berth,
                        b.cast_off_berth,
                        b.cast_off_port,

                        UPPER(COALESCE(b.cargo_name, '')) AS cargo_name

                    FROM ldud_barge_lines b

                    WHERE b.ldud_id = ANY(%s)
                    AND (
                        b.cast_off_port IS NULL
                        OR b.cast_off_port > %s
                    )

                    ORDER BY b.barge_name
                """, (ldud_ids, ws_str))

                barge_rows = cur.fetchall()

                print("\n========== RAW FETCHED BARGES ==========")
                print(f"Total rows fetched: {len(barge_rows)}")
                for r in barge_rows:
                    print(
                        f"  BARGE: {r['barge_name']!r:30s} | "
                        f"CARGO: {r['cargo_name']!r:20s} | "
                        f"cast_off_port: {r['cast_off_port']} | "
                        f"commence_discharge_berth: {r['commence_discharge_berth']} | "
                        f"cast_off_berth: {r['cast_off_berth']}"
                    )
                print("========================================\n")

                active_barges = set()
                cement_barge_set = set()

                for r in barge_rows:
                    barge_name = (r['barge_name'] or '').strip().upper()
                    cargo_name = (r['cargo_name'] or '').strip().upper()

                    if not barge_name:
                        continue

                    if r['cast_off_port']:
                        status = 'Non-Operational'
                    elif r['completed_discharge_berth'] and not r['cast_off_berth']:
                        status = 'waiting_empty_jetty'
                    elif r['commence_discharge_berth'] and not r['cast_off_berth']:
                        status = 'at_jetty'
                    elif r['along_side_berth'] and not r['commence_discharge_berth']:
                        status = 'waiting_discharge'
                    elif r['cast_off_mv'] and not r['along_side_berth']:
                        status = 'at_gull_loaded'
                    elif r['commenced_loading'] and not r['completed_loading']:
                        status = 'under_loading'
                    elif r['along_side_vessel'] and not r['commenced_loading']:
                        status = 'waiting_loading'
                    else:
                        status = None

                    print(f"  BARGE: {barge_name:30s} | STATUS: {status}")

                    ACTIVE_STATUSES = {
                        'at_jetty',
                        'at_gull_loaded',
                        'waiting_discharge',
                        'under_loading',
                    }

                    if status not in ACTIVE_STATUSES:
                        continue

                    base_barge = barge_name.split('/')[0].strip()

                    active_barges.add(base_barge)

                    if 'CLINKER' in cargo_name or 'SLAG' in cargo_name:
                        cement_barge_set.add(base_barge)

                total_barges = len(active_barges)
                cement_barges = len(cement_barge_set)
                steel_barges = total_barges - cement_barges
                barges_count = total_barges

                print("\n========== BARGE SUMMARY ==========")
                for b in sorted(active_barges):
                    print("BARGE:", b)
                print("TOTAL BARGES      :", total_barges)
                print("CEMENT BARGES     :", cement_barges)
                print("STEEL BARGES      :", steel_barges)
                print("FINAL BARGE COUNT :", barges_count)
                print("===================================\n")

        except Exception as e:
            import traceback
            print("BARGE ERROR:", str(e))
            traceback.print_exc()
            total_barges = 0
            cement_barges = 0
            steel_barges = 0
            barges_count = 0

        # =================================================
        # STEEL / CEMENT CARGO TOTAL
        # =================================================

        steel_cargo = 0
        cement_cargo = 0

        try:

            fetch_date = (
                datetime.strptime(
                    selected_date,
                    '%Y-%m-%d'
                ).date()
                - timedelta(days=1)
            )

            cur.execute("""
                SELECT

                    COALESCE(
                        vc.cargo_type,
                        'OTHERS'
                    ) AS cargo_type,

                    COALESCE(
                        SUM(l.quantity),
                        0
                    ) AS qty

                FROM lueu_lines l

                LEFT JOIN vessel_cargo vc
                    ON UPPER(TRIM(vc.cargo_name))
                    = UPPER(TRIM(l.cargo_name))

                WHERE
                    l.is_deleted = false
                    AND TO_DATE(
                        l.entry_date,
                        'YYYY-MM-DD'
                    ) = %s

                GROUP BY
                    COALESCE(
                        vc.cargo_type,
                        'OTHERS'
                    )

                ORDER BY 1

            """, (fetch_date,))

            cargo_rows = cur.fetchall()

            print("\n===== CARGO ROWS =====")

            total_qty = 0
            clinker_qty = 0
            slag_qty = 0

            for r in cargo_rows:

                cargo_type = str(
                    r['cargo_type']
                    or ''
                ).strip().upper()

                qty = float(
                    r['qty']
                    or 0
                )

                print(
                    "CARGO TYPE:",
                    cargo_type,
                    "QTY:",
                    qty
                )

                total_qty += qty

                if cargo_type == 'CLINKER':
                    clinker_qty += qty

                elif cargo_type == 'SLAG':
                    slag_qty += qty

            cement_cargo = (
                clinker_qty
                + slag_qty
            )

            steel_cargo = (
                total_qty
                - clinker_qty
                - slag_qty
            )

            print("========================")

            print(
                "TOTAL QTY:",
                total_qty
            )

            print(
                "CLINKER QTY:",
                clinker_qty
            )

            print(
                "SLAG QTY:",
                slag_qty
            )

            print(
                "CEMENT CARGO:",
                cement_cargo
            )

            print(
                "STEEL CARGO:",
                steel_cargo
            )

        except Exception as e:

            print(
                "CARGO TOTAL ERROR:",
                str(e)
            )

            import traceback
            traceback.print_exc()

            cement_cargo = 0
            steel_cargo = 0

        # =================================================
        # JETTY HANDLING
        # =================================================

        try:

            print("\n--- FETCHING JETTY HANDLING ---")

            target_date = fetch_date

            print("TARGET DATE:", target_date)

            month_start = date(
                target_date.year,
                target_date.month,
                1
            )

            if target_date.month >= 4:
                fy_start = date(
                    target_date.year,
                    4,
                    1
                )
            else:
                fy_start = date(
                    target_date.year - 1,
                    4,
                    1
                )

            cur.execute("""
                WITH hist AS (

                    SELECT
                        entry_date,
                        SUM(quantity) qty
                    FROM rp01_historical_lueu
                    WHERE cargo_name IS NOT NULL
                    GROUP BY entry_date

                ),

                live AS (

                    SELECT
                        TO_DATE(entry_date,'YYYY-MM-DD') AS entry_date,
                        SUM(quantity) qty
                    FROM lueu_lines
                    WHERE is_deleted = false
                    AND cargo_name IS NOT NULL
                    GROUP BY TO_DATE(entry_date,'YYYY-MM-DD')

                ),

                throughput AS (

                    SELECT
                        h.entry_date,
                        h.qty
                    FROM hist h

                    UNION ALL

                    SELECT
                        l.entry_date,
                        l.qty
                    FROM live l
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM hist h
                        WHERE h.entry_date = l.entry_date
                    )
                )

                SELECT

                    COALESCE(
                        SUM(
                            CASE
                                WHEN entry_date = %s
                                THEN qty
                                ELSE 0
                            END
                        ),
                        0
                    ) AS day_qty,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN entry_date BETWEEN %s AND %s
                                THEN qty
                                ELSE 0
                            END
                        ),
                        0
                    ) AS month_qty,

                    COALESCE(
                        SUM(
                            CASE
                                WHEN entry_date BETWEEN %s AND %s
                                THEN qty
                                ELSE 0
                            END
                        ),
                        0
                    ) AS year_qty

                FROM throughput

            """, (
                target_date,
                month_start,
                target_date,
                fy_start,
                target_date
            ))

            row = cur.fetchone()

            jetty_today = float(row['day_qty'] or 0)
            jetty_mtd = float(row['month_qty'] or 0)
            jetty_ytd = float(row['year_qty'] or 0)

            print("DAY QTY   :", jetty_today)
            print("MONTH QTY :", jetty_mtd)
            print("YEAR QTY  :", jetty_ytd)

        except Exception as e:

            print("JETTY ERROR:", str(e))

            conn.rollback()

            jetty_today = 0
            jetty_mtd = 0
            jetty_ytd = 0

        # -----------------------------------------
        # JETTY CARGO BREAKDOWN
        # -----------------------------------------

        # Previous day's data
        target_date = datetime.strptime(
            selected_date,
            '%Y-%m-%d'
        ).date() - timedelta(days=1)

        cur.execute("""
            SELECT
                l.cargo_name,
                COALESCE(SUM(l.quantity), 0) AS qty

            FROM lueu_lines l

            WHERE l.entry_date = %s
            AND l.quantity > 0
            AND l.cargo_name IS NOT NULL
            AND l.cargo_name != ''

            GROUP BY l.cargo_name

            ORDER BY qty DESC

        """, (target_date.strftime('%Y-%m-%d'),))

        jetty_cargo_list = [
            {
                'cargo_name': r['cargo_name'],
                'quantity': float(r['qty'] or 0)
            }
            for r in cur.fetchall()
        ]

        print("TARGET DATE:", target_date)
        print("JETTY CARGO LIST:", jetty_cargo_list)

        # =================================================
        # RHMS, NO CARGO, MAINTENANCE & CEMENT PLANT DELAYS
        # =================================================

        delay_rows = []

        rhms_delay_hours = 0
        no_cargo_hours = 0
        maintenance_delay_hours = 0
        cement_plant_delay_hours = 0

        try:

            print("\n--- FETCHING DELAYS ---")

            cur.execute("""
                SELECT
                    COALESCE(d.type, 'Other') AS delay_type,
                    l.delay_name,
                    l.from_time,
                    l.to_time
                FROM lueu_lines l
                LEFT JOIN port_delay_types d
                    ON d.name = l.delay_name
                WHERE l.entry_date = %s
                AND l.delay_name IS NOT NULL
                AND l.delay_name != ''
            """, (fetch_date.strftime('%Y-%m-%d'),))

            rows = cur.fetchall()

            for row in rows:

                delay_type = (row['delay_type'] or '').strip()
                delay_name = (row['delay_name'] or '').strip().upper()

                from_t = (row['from_time'] or '').strip()
                to_t = (row['to_time'] or '').strip()

                if not from_t or not to_t:
                    continue

                try:

                    start = datetime.strptime(from_t, '%H:%M')
                    end = datetime.strptime(to_t, '%H:%M')

                    minutes = (end - start).total_seconds() / 60

                    if minutes < 0:
                        minutes += (24 * 60)

                    hours = minutes / 60

                except Exception:
                    continue

                print(
                    "TYPE =", delay_type,
                    "| NAME =", delay_name,
                    "| HOURS =", hours
                )

                if delay_name == 'NO CARGO':

                    no_cargo_hours += hours

                elif (
                    delay_type.upper() == 'CEMENT PLANT DELAYS'
                    or 'CEMENT' in delay_name
                ):

                    cement_plant_delay_hours += hours

                elif delay_type == 'RMHS Delays':

                    rhms_delay_hours += hours

                elif delay_type == 'Maintenance Delays':

                    maintenance_delay_hours += hours

            rhms_delay_hours = round(rhms_delay_hours, 2)
            no_cargo_hours = round(no_cargo_hours, 2)
            maintenance_delay_hours = round(maintenance_delay_hours, 2)
            cement_plant_delay_hours = round(cement_plant_delay_hours, 2)

            delay_rows = [

                {
                    'delay_name': 'RHMS Delays',
                    'total_hours': rhms_delay_hours
                },

                {
                    'delay_name': 'No Cargo',
                    'total_hours': no_cargo_hours
                },

                {
                    'delay_name': 'Maintenance Delays',
                    'total_hours': maintenance_delay_hours
                },

                {
                    'delay_name': 'Cement Plant Delays',
                    'total_hours': cement_plant_delay_hours
                }
            ]

            print("RHMS DELAYS:", rhms_delay_hours)
            print("NO CARGO:", no_cargo_hours)
            print("MAINTENANCE DELAYS:", maintenance_delay_hours)
            print("CEMENT PLANT DELAYS:", cement_plant_delay_hours)

        except Exception as e:

            print("DELAY ERROR:", str(e))

            delay_rows = []

            rhms_delay_hours = 0
            no_cargo_hours = 0
            maintenance_delay_hours = 0
            cement_plant_delay_hours = 0

        response = {
        'success': True,

        'selected_date': datetime.strptime(
            selected_date,
            '%Y-%m-%d'
        ).strftime('%d/%m/%Y'),

        'fetch_date': fetch_date.strftime('%Y-%m-%d'),

        'mv_disch': str(mv_disch),
        'mv_total_days': str(mv_total_days),
        'mv_discharge_list': mv_discharge_list,
        'mv_waiting_list': mv_waiting_list,

        'barges_count': str(barges_count),
        'cement_barges': cement_barges,
        'steel_barges': steel_barges,
        'cement_cargo': round(cement_cargo, 2),
        'steel_cargo': round(steel_cargo, 2),

        'mbc_waiting': mbc_waiting_rows,
        'mbc_disch_total': str(mbc_disch_total),

        'jetty_today': str(jetty_today),
        'jetty_mtd': str(jetty_mtd),
        'jetty_ytd': str(jetty_ytd),

        'jetty_cargo_list': jetty_cargo_list,

        'rhms_delay_hours': rhms_delay_hours,
        'maintenance_delay_hours': maintenance_delay_hours,
        'no_cargo_hours': no_cargo_hours,
        'cement_plant_delay_hours': cement_plant_delay_hours,

        'delay_rows': delay_rows


        }


        print("\nRESPONSE CREATED SUCCESSFULLY")

        return jsonify(response)

    except Exception as e:

        print("\nMAIN ERROR:", repr(e))

        return jsonify({

            'success': False,
            'message': str(e)

        }), 500

    finally:

        print("\nCLOSING CONNECTION")

        if cur:
            cur.close()

        if conn:
            conn.close()