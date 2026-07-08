from database import get_db, get_cursor
from datetime import datetime, date, timedelta


def _hhmm_to_minutes(t):
    """'HH:MM' -> minutes since midnight, or None if not parseable."""
    if not t or not isinstance(t, str):
        return None
    parts = t.strip().split(':')
    if len(parts) < 2:
        return None
    try:
        h = int(parts[0]); m = int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _intervals_overlap(from_a, to_a, from_b, to_b):
    """True if two HH:MM ranges on the same date intersect.

    A range whose end <= start is treated as crossing midnight (end += 1440),
    matching the overnight convention used by calcDiffHrs in the template.
    Because the day is cyclic, a non-wrapping range is also compared against the
    other range shifted by +1440 so overnight overlaps are caught symmetrically
    (the result is independent of argument order).
    Returns False if either range is incomplete/unparseable.
    """
    fa = _hhmm_to_minutes(from_a); ta = _hhmm_to_minutes(to_a)
    fb = _hhmm_to_minutes(from_b); tb = _hhmm_to_minutes(to_b)
    if fa is None or ta is None or fb is None or tb is None:
        return False
    if ta <= fa:
        ta += 1440
    if tb <= fb:
        tb += 1440

    def _lin(s1, e1, s2, e2):
        return s1 < e2 and s2 < e1

    return (_lin(fa, ta, fb, tb)
            or _lin(fa, ta, fb + 1440, tb + 1440)
            or _lin(fa + 1440, ta + 1440, fb, tb))


def compute_rejections(data, limit_checks, overlap_candidates):
    """Pure decision logic for save-time validation.

    Args:
        data: the row dict about to be written.
        limit_checks: list of quantity-limit dicts the row must satisfy, each
            {'scope': str, 'label': str, 'expected': float, 'handled': float}.
            `handled` excludes this row; a check with expected <= 0 is skipped.
            The row's quantity is rejected against the TIGHTEST breached limit
            (smallest remaining), and the breached scope is reported so callers
            can tell the user / Excel which tier failed.
        overlap_candidates: list of (from_time, to_time) tuples for other rows on
            the same equipment + entry_date (already excludes this row).

    Returns (clean_data, rejections):
        clean_data: a copy of `data` with rejected fields set to None.
        rejections: list of dicts describing each rejected field.
    """
    clean = dict(data)
    rejections = []

    # ── Quantity vs the applicable limits (vessel/barge for VCN, header/cargo
    #    for MBC). Only the quantity is blanked; the rest of the row is kept. ──
    qty = clean.get('quantity')
    if qty is not None and str(qty).strip() != '':
        try:
            qv = float(qty)
        except (TypeError, ValueError):
            qv = None
        if qv is not None:
            breached = []
            for chk in (limit_checks or []):
                exp = chk.get('expected')
                if exp and float(exp) > 0:
                    remaining = float(exp) - float(chk.get('handled') or 0)
                    if qv > remaining + 1e-9:
                        breached.append((remaining, chk))
            if breached:
                breached.sort(key=lambda x: x[0])  # tightest (binding) limit first
                remaining, chk = breached[0]
                clean['quantity'] = None
                rejections.append({
                    'field': 'quantity',
                    'reason': 'exceeds_' + chk.get('scope', 'trip_qty'),
                    'scope': chk.get('scope', ''),
                    'scope_label': chk.get('label', ''),
                    'label': clean.get('barge_name') or clean.get('source_display') or '',
                    'attempted': round(qv, 3),
                    'remaining': round(remaining, 3),
                })

    # ── Time overlap vs same equipment + same date ───────────────────────────
    ft = clean.get('from_time'); tt = clean.get('to_time')
    if _hhmm_to_minutes(ft) is not None and _hhmm_to_minutes(tt) is not None:
        # From == To reads as a 24h overnight range everywhere (calcDiffHrs,
        # _intervals_overlap, reports) — reject it outright.
        if _hhmm_to_minutes(ft) == _hhmm_to_minutes(tt):
            clean['from_time'] = None
            clean['to_time'] = None
            rejections.append({'field': 'time', 'reason': 'zero_duration'})
            return clean, rejections
        for cf, ct in overlap_candidates:
            if _intervals_overlap(ft, tt, cf, ct):
                clean['from_time'] = None
                clean['to_time'] = None
                rejections.append({
                    'field': 'time',
                    'reason': 'overlap',
                    'conflict': {'from_time': cf, 'to_time': ct},
                })
                break

    return clean, rejections


def _resolve_trip_limits(cur, data, exclude_id):
    """Return the list of quantity-limit checks the row must satisfy.

    Each item: {'scope', 'label', 'expected', 'handled'} where `handled` excludes
    this row (exclude_id). A check with expected <= 0 is skipped by
    compute_rejections, so an empty/zero basis simply means 'no check'.

    VCN rows are limited by two tiers:
      - 'vessel': total handled across the whole VCN vs the sum of BL across the
                  vessel's cargo declarations (import + export).
      - 'barge' : handled for this barge/trip vs the barge line discharge_quantity.
    MBC rows are limited by two tiers:
      - 'bl_header': total handled across the whole MBC vs mbc_header.bl_quantity.
      - 'cargo'    : handled for this row's cargo vs the summed customer-details
                     quantity for that cargo.
    `exclude_id` is the row's own id (None for new rows); it is excluded from the
    handled sums so re-saving an existing row never double-counts itself.
    """
    source_type = data.get('source_type')
    source_id = data.get('source_id')
    if not source_type or not source_id:
        return []

    checks = []

    if source_type == 'VCN':
        # ── Tier 1: whole-vessel BL (sum of import + export cargo declarations) ──
        cur.execute('''
            SELECT COALESCE((SELECT SUM(bl_quantity) FROM vcn_cargo_declaration WHERE vcn_id = %s), 0)
                 + COALESCE((SELECT SUM(bl_quantity) FROM vcn_export_cargo_declaration WHERE vcn_id = %s), 0)
                   AS vessel_bl
        ''', [source_id, source_id])
        vessel_bl = float(cur.fetchone()['vessel_bl'] or 0)
        cur.execute('''
            SELECT COALESCE(SUM(quantity), 0) AS handled
            FROM lueu_lines
            WHERE source_type = 'VCN' AND source_id = %s
              AND (is_deleted IS NOT TRUE) AND id != %s
        ''', [source_id, exclude_id or 0])
        vessel_handled = float(cur.fetchone()['handled'] or 0)
        checks.append({'scope': 'vessel', 'label': 'vessel quantity',
                       'expected': vessel_bl, 'handled': vessel_handled})

        # ── Tier 2: this barge/trip's discharge_quantity ──
        barge_name = data.get('barge_name')
        if barge_name:
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
                expected = 0.0
                for r in cur.fetchall():
                    trip = r['trip_number'] or ''
                    display = f"{r['barge_name']} / {trip}" if trip else r['barge_name']
                    if display == barge_name:
                        expected = float(r['expected_qty'] or 0)
                        break
                cur.execute('''
                    SELECT COALESCE(SUM(quantity), 0) AS handled
                    FROM lueu_lines
                    WHERE source_type = 'VCN' AND source_id = %s AND barge_name = %s
                      AND (is_deleted IS NOT TRUE) AND id != %s
                ''', [source_id, barge_name, exclude_id or 0])
                handled = float(cur.fetchone()['handled'] or 0)
                checks.append({'scope': 'barge', 'label': 'barge quantity',
                               'expected': expected, 'handled': handled})
        return checks

    if source_type == 'MBC':
        # ── Tier 1: header BL quantity ──
        cur.execute('SELECT COALESCE(bl_quantity, 0) AS bl FROM mbc_header WHERE id = %s', [source_id])
        row = cur.fetchone()
        header_bl = float(row['bl'] or 0) if row else 0.0
        cur.execute('''
            SELECT COALESCE(SUM(quantity), 0) AS handled
            FROM lueu_lines
            WHERE source_type = 'MBC' AND source_id = %s
              AND (is_deleted IS NOT TRUE) AND id != %s
        ''', [source_id, exclude_id or 0])
        total_handled = float(cur.fetchone()['handled'] or 0)
        checks.append({'scope': 'bl_header', 'label': 'BL header quantity',
                       'expected': header_bl, 'handled': total_handled})

        # ── Tier 2: this cargo's customer-details quantity ──
        cargo_name = data.get('cargo_name')
        if cargo_name:
            cur.execute('''
                SELECT COALESCE(SUM(quantity), 0) AS cargo_qty
                FROM mbc_customer_details
                WHERE mbc_id = %s AND cargo_name = %s
            ''', [source_id, cargo_name])
            cargo_qty = float(cur.fetchone()['cargo_qty'] or 0)
            cur.execute('''
                SELECT COALESCE(SUM(quantity), 0) AS handled
                FROM lueu_lines
                WHERE source_type = 'MBC' AND source_id = %s AND cargo_name = %s
                  AND (is_deleted IS NOT TRUE) AND id != %s
            ''', [source_id, cargo_name, exclude_id or 0])
            cargo_handled = float(cur.fetchone()['handled'] or 0)
            checks.append({'scope': 'cargo', 'label': 'customer cargo quantity',
                           'expected': cargo_qty, 'handled': cargo_handled})
        return checks

    return []


def _overlap_candidates(cur, data, exclude_id):
    """Return [(from_time, to_time), ...] for other rows on the same
    equipment_name + entry_date that have both times set (excludes this row)."""
    equipment_name = data.get('equipment_name')
    entry_date = data.get('entry_date')
    if not equipment_name or not entry_date:
        return []
    cur.execute('''
        SELECT from_time, to_time FROM lueu_lines
        WHERE equipment_name = %s AND entry_date = %s
          AND (is_deleted IS NOT TRUE) AND id != %s
          AND from_time IS NOT NULL AND from_time != ''
          AND to_time IS NOT NULL AND to_time != ''
    ''', [equipment_name, entry_date, exclude_id or 0])
    return [(r['from_time'], r['to_time']) for r in cur.fetchall()]


def _diff_hrs(from_t, to_t):
    """Duration in hours between two HH:MM strings, overnight-aware (matches the
    client calcDiffHrs). Returns '' when either time is missing/unparseable."""
    f = _hhmm_to_minutes(from_t); t = _hhmm_to_minutes(to_t)
    if f is None or t is None:
        return ''
    diff = (t - f) if t > f else (1440 - f + t)
    return round(diff / 60.0, 2)


def _over_group_key(r):
    """Barge/MBC over-quantity group key for a row: VCN → (type, id, barge),
    MBC → (type, id, None); None when the row has no source to check."""
    st = r.get('source_type'); sid = r.get('source_id')
    if st == 'VCN' and sid and r.get('barge_name'):
        return (st, sid, r['barge_name'])
    if st == 'MBC' and sid:
        return (st, sid, None)
    return None


def _over_quantity_by(cur, rows):
    """Return {row_id: {'scopes': [label, ...], 'over_by': float}} for `rows`.

    A scope is present for a row when its group's TOTAL non-deleted handled qty
    exceeds the corresponding limit (the same two tiers _resolve_trip_limits
    enforces at save time):
      VCN → 'vessel quantity' (whole VCN vs cargo-declaration BL),
            'barge quantity'  (barge/trip vs barge-line discharge qty).
      MBC → 'BL header quantity'      (whole MBC vs header BL),
            'customer cargo quantity' (per-cargo vs customer-details qty).
    `over_by` is the largest single overflow among the row's breached scopes.
    Batched; shared by get_all_lines (on-screen badge) and the Excel export.
    """
    vcn_ids = {r['source_id'] for r in rows
               if r.get('source_type') == 'VCN' and r.get('source_id')}
    mbc_ids = {r['source_id'] for r in rows
               if r.get('source_type') == 'MBC' and r.get('source_id')}

    vcn_vessel_bl = {}      # vcn_id -> total cargo-declaration BL
    vcn_total_handled = {}  # vcn_id -> total handled across the VCN
    vcn_barge_expected = {} # (vcn_id, barge_display) -> barge discharge qty
    vcn_barge_handled = {}  # (vcn_id, barge_name)     -> handled for that barge
    if vcn_ids:
        ph = ','.join(['%s'] * len(vcn_ids))
        ids = list(vcn_ids)
        cur.execute(f'''
            SELECT vcn_id, COALESCE(SUM(bl_quantity), 0) AS bl FROM (
                SELECT vcn_id, bl_quantity FROM vcn_cargo_declaration WHERE vcn_id IN ({ph})
                UNION ALL
                SELECT vcn_id, bl_quantity FROM vcn_export_cargo_declaration WHERE vcn_id IN ({ph})
            ) t GROUP BY vcn_id
        ''', ids + ids)
        for r in cur.fetchall():
            vcn_vessel_bl[r['vcn_id']] = float(r['bl'] or 0)
        cur.execute(f'''
            SELECT source_id, COALESCE(SUM(quantity), 0) AS handled
            FROM lueu_lines
            WHERE source_type = 'VCN' AND source_id IN ({ph}) AND (is_deleted IS NOT TRUE)
            GROUP BY source_id
        ''', ids)
        for r in cur.fetchall():
            vcn_total_handled[r['source_id']] = float(r['handled'] or 0)
        # one ldud_header per vcn (first wins, mirroring _resolve_trip_limits)
        cur.execute(f'SELECT id, vcn_id FROM ldud_header WHERE vcn_id IN ({ph})', ids)
        first_lduds = {}   # ldud_id -> vcn_id
        seen_vcn = set()
        for lr in cur.fetchall():
            if lr['vcn_id'] not in seen_vcn:
                seen_vcn.add(lr['vcn_id'])
                first_lduds[lr['id']] = lr['vcn_id']
        if first_lduds:
            lph = ','.join(['%s'] * len(first_lduds))
            cur.execute(f'''
                SELECT ldud_id, barge_name, trip_number,
                       COALESCE(SUM(discharge_quantity), 0) AS expected_qty
                FROM ldud_barge_lines
                WHERE ldud_id IN ({lph}) AND barge_name IS NOT NULL AND barge_name != ''
                GROUP BY ldud_id, barge_name, trip_number
            ''', list(first_lduds.keys()))
            for br in cur.fetchall():
                vcn_id = first_lduds.get(br['ldud_id'])
                if vcn_id is None:
                    continue
                trip = br['trip_number'] or ''
                display = f"{br['barge_name']} / {trip}" if trip else br['barge_name']
                vcn_barge_expected[(vcn_id, display)] = float(br['expected_qty'] or 0)
        cur.execute(f'''
            SELECT source_id, barge_name, COALESCE(SUM(quantity), 0) AS handled
            FROM lueu_lines
            WHERE source_type = 'VCN' AND source_id IN ({ph})
              AND (is_deleted IS NOT TRUE) AND barge_name IS NOT NULL
            GROUP BY source_id, barge_name
        ''', ids)
        for hr in cur.fetchall():
            vcn_barge_handled[(hr['source_id'], hr['barge_name'])] = float(hr['handled'] or 0)

    mbc_header_bl = {}      # mbc_id -> header BL qty
    mbc_total_handled = {}  # mbc_id -> total handled across the MBC
    mbc_cargo_qty = {}      # (mbc_id, cargo) -> customer-details qty for that cargo
    mbc_cargo_handled = {}  # (mbc_id, cargo) -> handled for that cargo
    if mbc_ids:
        ph = ','.join(['%s'] * len(mbc_ids))
        ids = list(mbc_ids)
        cur.execute(f'SELECT id, COALESCE(bl_quantity, 0) AS bl FROM mbc_header WHERE id IN ({ph})', ids)
        for mr in cur.fetchall():
            mbc_header_bl[mr['id']] = float(mr['bl'] or 0)
        cur.execute(f'''
            SELECT source_id, COALESCE(SUM(quantity), 0) AS handled
            FROM lueu_lines
            WHERE source_type = 'MBC' AND source_id IN ({ph}) AND (is_deleted IS NOT TRUE)
            GROUP BY source_id
        ''', ids)
        for hr in cur.fetchall():
            mbc_total_handled[hr['source_id']] = float(hr['handled'] or 0)
        cur.execute(f'''
            SELECT mbc_id, cargo_name, COALESCE(SUM(quantity), 0) AS qty
            FROM mbc_customer_details
            WHERE mbc_id IN ({ph}) AND cargo_name IS NOT NULL
            GROUP BY mbc_id, cargo_name
        ''', ids)
        for mr in cur.fetchall():
            mbc_cargo_qty[(mr['mbc_id'], mr['cargo_name'])] = float(mr['qty'] or 0)
        cur.execute(f'''
            SELECT source_id, cargo_name, COALESCE(SUM(quantity), 0) AS handled
            FROM lueu_lines
            WHERE source_type = 'MBC' AND source_id IN ({ph})
              AND (is_deleted IS NOT TRUE) AND cargo_name IS NOT NULL
            GROUP BY source_id, cargo_name
        ''', ids)
        for hr in cur.fetchall():
            mbc_cargo_handled[(hr['source_id'], hr['cargo_name'])] = float(hr['handled'] or 0)

    over = {}
    for r in rows:
        rid = r.get('id')
        st = r.get('source_type'); sid = r.get('source_id')
        scopes = []
        over_by = 0.0
        if st == 'VCN' and sid:
            vbl = vcn_vessel_bl.get(sid, 0); vh = vcn_total_handled.get(sid, 0)
            if vbl > 0 and vh > vbl:
                scopes.append('vessel quantity'); over_by = max(over_by, vh - vbl)
            barge = r.get('barge_name')
            if barge:
                be = vcn_barge_expected.get((sid, barge), 0)
                bh = vcn_barge_handled.get((sid, barge), 0)
                if be > 0 and bh > be:
                    scopes.append('barge quantity'); over_by = max(over_by, bh - be)
        elif st == 'MBC' and sid:
            hbl = mbc_header_bl.get(sid, 0); th = mbc_total_handled.get(sid, 0)
            if hbl > 0 and th > hbl:
                scopes.append('BL header quantity'); over_by = max(over_by, th - hbl)
            cargo = r.get('cargo_name')
            if cargo:
                cq = mbc_cargo_qty.get((sid, cargo), 0)
                ch = mbc_cargo_handled.get((sid, cargo), 0)
                if cq > 0 and ch > cq:
                    scopes.append('customer cargo quantity'); over_by = max(over_by, ch - cq)
        if scopes and rid is not None:
            over[rid] = {'scopes': scopes, 'over_by': round(over_by, 3)}
    return over


def get_all_lines(page=1, size=20, equipment_name=None, filters=None):
    conn = get_db()
    cur = get_cursor(conn)
    offset = (page - 1) * size

    allowed = {'entry_date', 'shift', 'source_display', 'barge_name', 'cargo_name',
               'delay_name', 'berth_name', 'operator_name', 'route_name'}
    where_clauses, params = [], []

    if equipment_name:
        where_clauses.append('equipment_name = %s')
        params.append(equipment_name)

    for f in (filters or []):
        field = f.get('field', '')
        if field not in allowed:
            continue
        ftype = f.get('type')
        if ftype == 'contains' and f.get('value'):
            where_clauses.append(f"{field} ILIKE %s")
            params.append(f"%{f['value']}%")
        elif ftype == 'multi' and f.get('values'):
            ph = ','.join(['%s'] * len(f['values']))
            where_clauses.append(f"{field} IN ({ph})")
            params.extend(f['values'])
        elif ftype == 'range':
            if f.get('from'):
                where_clauses.append(f"{field} >= %s")
                params.append(f['from'])
            if f.get('to'):
                where_clauses.append(f"{field} <= %s")
                params.append(f['to'])

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    cur.execute(f'SELECT COUNT(*) as cnt FROM lueu_lines {where_sql}', params)
    total = cur.fetchone()['cnt']
    # Chronological, newest-first, honouring the shift-date convention:
    # entry_date is the shift START date, so C-shift times before noon belong
    # past midnight (+1440). Plain from_time DESC was tried and reverted
    # (90e7619) because it sank those post-midnight C rows below A/B rows.
    order_sql = '''ORDER BY entry_date DESC,
                 CASE WHEN from_time ~ '^[0-2][0-9]:[0-5][0-9]$' THEN
                      CAST(SPLIT_PART(from_time, ':', 1) AS INT) * 60
                    + CAST(SPLIT_PART(from_time, ':', 2) AS INT)
                    + CASE WHEN shift = 'C' AND from_time < '12:00' THEN 1440 ELSE 0 END
                 ELSE -1 END DESC,
                 id DESC'''
    cur.execute(f'SELECT * FROM lueu_lines {where_sql} {order_sql} LIMIT %s OFFSET %s',
                params + [size, offset])
    rows = [dict(r) for r in cur.fetchall()]

    # Look up customer names from cargo declarations for VCN/MBC sources
    # and flag multi-customer sources
    source_keys = set()
    for r in rows:
        if r.get('source_type') and r.get('source_id'):
            source_keys.add((r['source_type'], r['source_id']))

    # Build map of (source_type, source_id) -> {cargo_name: customer_name}
    source_customer_map = {}  # key -> {cargo_name: customer_name}
    source_multi_customer = {}  # key -> bool (has multiple customers)

    for src_type, src_id in source_keys:
        cargo_customers = {}
        if src_type == 'VCN':
            cur.execute("""
                SELECT cargo_name, customer_name FROM vcn_cargo_declaration WHERE vcn_id = %s AND customer_name IS NOT NULL
                UNION ALL
                SELECT cargo_name, customer_name FROM vcn_export_cargo_declaration WHERE vcn_id = %s AND customer_name IS NOT NULL
            """, [src_id, src_id])
            for cr in cur.fetchall():
                if cr['cargo_name'] and cr['customer_name']:
                    cargo_customers[cr['cargo_name']] = cr['customer_name']
        elif src_type == 'MBC':
            cur.execute("""
                SELECT cargo_name, customer_name FROM mbc_customer_details WHERE mbc_id = %s AND customer_name IS NOT NULL
            """, [src_id])
            for cr in cur.fetchall():
                if cr['customer_name']:
                    cargo_customers[cr.get('cargo_name') or '_all'] = cr['customer_name']

        source_customer_map[(src_type, src_id)] = cargo_customers
        unique_customers = set(cargo_customers.values())
        source_multi_customer[(src_type, src_id)] = len(unique_customers) > 1

    # Enrich rows with customer_name (only when multi-customer)
    for r in rows:
        key = (r.get('source_type'), r.get('source_id'))
        is_multi = source_multi_customer.get(key, False)
        r['_multi_customer'] = is_multi
        if is_multi:
            cargo_customers = source_customer_map.get(key, {})
            r['customer_name'] = cargo_customers.get(r.get('cargo_name'), '')
        else:
            r['customer_name'] = ''

    # Flag historical over-quantity rows: every row of a barge/MBC whose TOTAL
    # handled qty exceeds its trip qty (same per-barge/MBC limit the save-time
    # validation enforces, so a flagged row is exactly one the rule would block).
    over_map = _over_quantity_by(cur, rows)
    for r in rows:
        info = over_map.get(r.get('id'))
        r['_qty_over_group'] = bool(info)
        r['_qty_over_by'] = info['over_by'] if info else 0
        r['_qty_over_scopes'] = info['scopes'] if info else []

    conn.close()

    return {
        'data': rows,
        'last_page': (total + size - 1) // size,
        'total': total
    }


def get_export_rows():
    """Return every lueu_lines row (including soft-deleted) for the Excel export,
    each enriched with `diff_hrs` and an `issues` string flagging
    'possible over quantity' and/or 'possible overlap'.

    Issues are computed only for live (non-deleted) rows:
      - over quantity: the row's barge/MBC group total handled exceeds its trip qty
        (same rule as the on-screen OVER badge).
      - overlap: the row's From-To intersects another live row on the same
        equipment + entry_date (same _intervals_overlap rule the save guard uses).
    """
    from collections import defaultdict

    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM lueu_lines ORDER BY equipment_name, entry_date, id')
    rows = [dict(r) for r in cur.fetchall()]

    live = [r for r in rows if not r.get('is_deleted')]
    over_map = _over_quantity_by(cur, live)
    conn.close()

    # Time overlaps among live rows, bucketed by equipment + entry_date.
    buckets = defaultdict(list)
    for r in live:
        if not r.get('equipment_name') or not r.get('entry_date'):
            continue
        if _hhmm_to_minutes(r.get('from_time')) is None or _hhmm_to_minutes(r.get('to_time')) is None:
            continue
        buckets[(r['equipment_name'], r['entry_date'])].append(r)
    overlap_ids = set()
    for grp in buckets.values():
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                if _intervals_overlap(grp[i]['from_time'], grp[i]['to_time'],
                                      grp[j]['from_time'], grp[j]['to_time']):
                    overlap_ids.add(grp[i]['id'])
                    overlap_ids.add(grp[j]['id'])

    for r in rows:
        r['diff_hrs'] = _diff_hrs(r.get('from_time'), r.get('to_time'))
        issues = []
        if not r.get('is_deleted'):
            info = over_map.get(r.get('id'))
            if info:
                for scope_label in info['scopes']:
                    issues.append('over ' + scope_label)
            if r.get('id') in overlap_ids:
                issues.append('possible overlap')
        r['issues'] = '; '.join(issues)
    return rows


def save_line(data):
    conn = get_db()
    cur = get_cursor(conn)

    # Coerce blank strings to None for numeric columns — Postgres rejects '' on real/integer.
    def _num(key):
        v = data.get(key)
        if v is None or (isinstance(v, str) and v.strip() == ''):
            return None
        return v
    data['quantity']  = _num('quantity')
    data['source_id'] = _num('source_id')

    line_id = data.get('id')

    # ── Validation: blank (but reject) over-limit quantity and overlapping times ──
    limit_checks = _resolve_trip_limits(cur, data, line_id)
    overlap = _overlap_candidates(cur, data, line_id)
    data, rejections = compute_rejections(data, limit_checks, overlap)

    if line_id:
        cur.execute('''
            UPDATE lueu_lines SET
                source_type = %s, source_id = %s, source_display = %s, barge_name = %s,
                equipment_name = %s, operator_name = %s, delay_name = %s, cargo_name = %s,
                operation_type = %s, quantity = %s, quantity_uom = %s, route_name = %s,
                start_time = %s, end_time = %s, entry_date = %s,
                shift = %s, from_time = %s, to_time = %s, system_name = %s,
                berth_name = %s, shift_incharge = %s, remarks = %s
            WHERE id = %s
        ''', [
            data.get('source_type'), data.get('source_id'), data.get('source_display'),
            data.get('barge_name'), data.get('equipment_name'), data.get('operator_name'),
            data.get('delay_name'), data.get('cargo_name'), data.get('operation_type'),
            data.get('quantity'), data.get('quantity_uom'), data.get('route_name'),
            data.get('start_time'), data.get('end_time'), data.get('entry_date'),
            data.get('shift'), data.get('from_time'), data.get('to_time'), data.get('system_name'),
            data.get('berth_name'), data.get('shift_incharge'), data.get('remarks'), line_id
        ])
    else:
        cur.execute('''
            INSERT INTO lueu_lines
            (source_type, source_id, source_display, barge_name, equipment_name, operator_name,
             delay_name, cargo_name, operation_type, quantity, quantity_uom, route_name,
             start_time, end_time, entry_date, created_by, created_date,
             shift, from_time, to_time, system_name, berth_name, shift_incharge, remarks)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', [
            data.get('source_type'), data.get('source_id'), data.get('source_display'),
            data.get('barge_name'), data.get('equipment_name'), data.get('operator_name'),
            data.get('delay_name'), data.get('cargo_name'), data.get('operation_type'),
            data.get('quantity'), data.get('quantity_uom'), data.get('route_name'),
            data.get('start_time'), data.get('end_time'), data.get('entry_date'),
            data.get('created_by'), datetime.now().strftime('%Y-%m-%d'),
            data.get('shift'), data.get('from_time'), data.get('to_time'), data.get('system_name'),
            data.get('berth_name'), data.get('shift_incharge'), data.get('remarks')
        ])
        line_id = cur.fetchone()['id']

    conn.commit()
    conn.close()
    return {'id': line_id, 'rejections': rejections}


def soft_delete_lines(ids, username=None):
    """Soft-delete lueu lines. Returns empty list (billing no longer tracked via lueu_lines)."""
    conn = get_db()
    cur = get_cursor(conn)
    today = datetime.now().strftime('%Y-%m-%d')
    for line_id in ids:
        cur.execute('''
            UPDATE lueu_lines
            SET is_deleted = TRUE, deleted_by = %s, deleted_date = %s
            WHERE id = %s AND (is_deleted IS NOT TRUE)
        ''', [username, today, line_id])
    conn.commit()
    conn.close()
    return []   # caller checks for invoiced_lines to trigger auto-CN; none here


def split_line(line_id, split_qty, split_remark, created_by=None):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM lueu_lines WHERE id = %s', [line_id])
    parent = cur.fetchone()
    if not parent:
        conn.close()
        return None
    parent = dict(parent)
    original_qty = float(parent.get('quantity') or 0)
    split_qty = float(split_qty)
    remaining_qty = original_qty - split_qty

    # Mark parent as split, update its quantity to the remaining
    cur.execute('''UPDATE lueu_lines SET is_split = TRUE, quantity = %s WHERE id = %s''',
                [remaining_qty, line_id])

    # Create child line with split quantity
    cur.execute('''
        INSERT INTO lueu_lines
        (source_type, source_id, source_display, barge_name, equipment_name, operator_name,
         delay_name, cargo_name, operation_type, quantity, quantity_uom, route_name,
         start_time, end_time, entry_date, created_by, created_date,
         shift, from_time, to_time, system_name, berth_name, shift_incharge, remarks,
         is_split, parent_line_id, split_quantity, split_remark)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s)
        RETURNING id
    ''', [
        parent.get('source_type'), parent.get('source_id'), parent.get('source_display'),
        parent.get('barge_name'), parent.get('equipment_name'), parent.get('operator_name'),
        parent.get('delay_name'), parent.get('cargo_name'), parent.get('operation_type'),
        split_qty, parent.get('quantity_uom'), parent.get('route_name'),
        parent.get('start_time'), parent.get('end_time'), parent.get('entry_date'),
        created_by, datetime.now().strftime('%Y-%m-%d'),
        parent.get('shift'), parent.get('from_time'), parent.get('to_time'),
        parent.get('system_name'), parent.get('berth_name'), parent.get('shift_incharge'),
        split_remark or parent.get('remarks'),
        line_id, split_qty, split_remark
    ])
    child_id = cur.fetchone()['id']

    # Also update parent's split fields
    cur.execute('''UPDATE lueu_lines SET split_quantity = %s, split_remark = %s WHERE id = %s''',
                [remaining_qty, 'Parent line (split)', line_id])

    conn.commit()
    conn.close()
    return {'child_id': child_id, 'parent_qty': remaining_qty, 'child_qty': split_qty}


def get_vcn_options():
    """Get VCN entries with vessel name and anchored time for dropdown."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT h.id, h.vcn_doc_num, h.vessel_name, a.anchorage_arrival
        FROM vcn_header h
        LEFT JOIN vcn_anchorage a ON h.id = a.vcn_id
        ORDER BY h.vcn_doc_num DESC
    ''')
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_mbc_options():
    """Get MBC entries for dropdown — includes all MBCs, flagging each with
    `completed` when LUEU handled qty >= BL qty (no longer hidden)."""
    conn = get_db()
    cur = get_cursor(conn)

    # BL quantity per MBC (customer_details sum if rows exist, else header bl_quantity)
    cur.execute('''
        SELECT m.id, m.doc_num, m.mbc_name, m.doc_date, m.cargo_name,
               CASE WHEN COUNT(cd.id) > 0 THEN COALESCE(SUM(cd.quantity), 0)
                    ELSE COALESCE(m.bl_quantity, 0) END AS bl_qty
        FROM mbc_header m
        LEFT JOIN mbc_customer_details cd ON cd.mbc_id = m.id
        GROUP BY m.id, m.doc_num, m.mbc_name, m.doc_date, m.cargo_name, m.bl_quantity
        ORDER BY m.doc_num DESC
    ''')
    mbcs = cur.fetchall()

    # LUEU handled quantity per MBC
    cur.execute('''
        SELECT source_id, COALESCE(SUM(quantity), 0) AS handled_qty
        FROM lueu_lines
        WHERE source_type = 'MBC' AND (is_deleted IS NOT TRUE)
        GROUP BY source_id
    ''')
    handled_map = {r['source_id']: float(r['handled_qty'] or 0) for r in cur.fetchall()}

    conn.close()

    # Keep fully-handled MBCs in the list (so users can still find them) but flag
    # them as completed — over-entry is prevented by save-time validation, not by
    # hiding the option.
    result = []
    for m in mbcs:
        bl = float(m['bl_qty'] or 0)
        handled = handled_map.get(m['id'], 0)
        row = dict(m)
        row['completed'] = bool(bl > 0 and handled >= bl)
        result.append(row)
    return result


def get_vcn_barges(vcn_id):
    """Get barge trips for a VCN as [{name, completed}, ...].

    Includes every trip; `completed` is True when handled qty >= discharge_quantity
    (such trips are flagged in the dropdown rather than hidden, so users can still
    find them). Aggregates by (barge_name, trip_number) so duplicate LDUD rows
    (same barge + trip) collapse. Sorted by data-entry order (earliest id first)
    rather than trip number, which previously confused users when trips were entered
    out of sequence.
    """
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT id FROM ldud_header WHERE vcn_id = %s', [vcn_id])
    ldud = cur.fetchone()
    if ldud:
        ldud_id = ldud['id']
        cur.execute('''
            SELECT barge_name,
                   trip_number,
                   COALESCE(SUM(discharge_quantity), 0) AS expected_qty,
                   MIN(id) AS first_id
            FROM ldud_barge_lines
            WHERE ldud_id = %s AND barge_name IS NOT NULL AND barge_name != ''
            GROUP BY barge_name, trip_number
            ORDER BY MIN(id) ASC
        ''', [ldud_id])
        trip_rows = cur.fetchall()

        # Handled quantity per barge/trip display label in LUEU
        cur.execute('''
            SELECT barge_name, COALESCE(SUM(quantity), 0) AS handled_qty
            FROM lueu_lines
            WHERE source_type = 'VCN' AND source_id = %s AND (is_deleted IS NOT TRUE)
              AND barge_name IS NOT NULL
            GROUP BY barge_name
        ''', [vcn_id])
        handled_map = {r['barge_name']: float(r['handled_qty'] or 0) for r in cur.fetchall()}

        conn.close()
        # Keep fully-handled barge trips in the list (flagged completed) rather
        # than hiding them — over-entry is blocked by save-time validation.
        result = []
        for r in trip_rows:
            trip = r['trip_number'] or ''
            display = f"{r['barge_name']} / {trip}" if trip else r['barge_name']
            expected = float(r['expected_qty'] or 0)
            handled = handled_map.get(display, 0)
            result.append({
                'name': display,
                'completed': bool(expected > 0 and handled >= expected),
            })
        return result
    conn.close()
    return []


def get_mbc_names():
    """Get all MBC names from master"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT mbc_name FROM mbc_master ORDER BY mbc_name')
    rows = cur.fetchall()
    conn.close()
    return [r['mbc_name'] for r in rows]


def get_bl_progress(source_type, source_id):
    """Return BL declared qty vs handled qty per cargo for a VCN or MBC source."""
    conn = get_db()
    cur = get_cursor(conn)

    declared = []
    if source_type == 'VCN':
        cur.execute('''
            SELECT COALESCE(cargo_name, '') as cargo_name, COALESCE(bl_quantity, 0) as bl_qty, 'Import' as decl_type
            FROM vcn_cargo_declaration WHERE vcn_id = %s
        ''', [source_id])
        for r in cur.fetchall():
            declared.append({'cargo_name': r['cargo_name'], 'bl_qty': float(r['bl_qty'] or 0), 'decl_type': 'Import'})
        cur.execute('''
            SELECT COALESCE(cargo_name, '') as cargo_name, COALESCE(bl_quantity, 0) as bl_qty, 'Export' as decl_type
            FROM vcn_export_cargo_declaration WHERE vcn_id = %s
        ''', [source_id])
        for r in cur.fetchall():
            declared.append({'cargo_name': r['cargo_name'], 'bl_qty': float(r['bl_qty'] or 0), 'decl_type': 'Export'})
    elif source_type == 'MBC':
        cur.execute('''
            SELECT COALESCE(cargo_name, '') as cargo_name, COALESCE(quantity, 0) as bl_qty
            FROM mbc_customer_details WHERE mbc_id = %s
        ''', [source_id])
        for r in cur.fetchall():
            declared.append({'cargo_name': r['cargo_name'], 'bl_qty': float(r['bl_qty'] or 0), 'decl_type': 'MBC'})

    # Sum handled quantities from lueu_lines per cargo (exclude deleted)
    cur.execute('''
        SELECT COALESCE(cargo_name, '') as cargo_name,
               COALESCE(SUM(quantity), 0) as handled_qty,
               MAX(quantity_uom) as uom
        FROM lueu_lines
        WHERE source_type = %s AND source_id = %s AND (is_deleted IS NOT TRUE)
        GROUP BY cargo_name
    ''', [source_type, source_id])
    handled_map = {}
    uom_map = {}
    for r in cur.fetchall():
        handled_map[r['cargo_name']] = float(r['handled_qty'] or 0)
        uom_map[r['cargo_name']] = r['uom'] or ''

    conn.close()

    result = []
    seen = set()
    for d in declared:
        cargo = d['cargo_name']
        bl_qty = d['bl_qty']
        handled = handled_map.get(cargo, 0)
        seen.add(cargo)
        result.append({
            'cargo_name': cargo,
            'bl_qty': round(bl_qty, 3),
            'handled_qty': round(handled, 3),
            'uom': uom_map.get(cargo, ''),
            'remaining': round(bl_qty - handled, 3),
            'exceeded': handled > bl_qty and bl_qty > 0,
            'exceeded_by': round(max(0.0, handled - bl_qty), 3),
            'decl_type': d.get('decl_type', ''),
        })

    # Handled cargos with no declaration at all
    for cargo, handled in handled_map.items():
        if cargo not in seen:
            result.append({
                'cargo_name': cargo,
                'bl_qty': 0,
                'handled_qty': round(handled, 3),
                'uom': uom_map.get(cargo, ''),
                'remaining': round(-handled, 3),
                'exceeded': True,
                'exceeded_by': round(handled, 3),
                'decl_type': 'No Declaration',
            })

    return result


def get_barge_cargos(vcn_id, barge_name):
    """Get cargo names for a specific barge from a VCN's LDUD"""
    # Strip trip number if barge_name is in "barge / trip" format
    if ' / ' in barge_name:
        barge_name = barge_name.split(' / ')[0].strip()
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT id FROM ldud_header WHERE vcn_id = %s', [vcn_id])
    ldud = cur.fetchone()
    cargos = []
    if ldud:
        cur.execute('''
            SELECT DISTINCT cargo_name FROM ldud_barge_lines
            WHERE ldud_id = %s AND barge_name = %s AND cargo_name IS NOT NULL AND cargo_name != ''
        ''', [ldud['id'], barge_name])
        cargos = [r['cargo_name'] for r in cur.fetchall()]
    conn.close()
    return cargos


def get_dashboard_data():
    """Return all data for the LUEU01 operations dashboard."""
    conn = get_db()
    cur = get_cursor(conn)

    today = date.today()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)
    # Financial year starts April 1
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)

    today_s     = today.strftime('%Y-%m-%d')
    yesterday_s = yesterday.strftime('%Y-%m-%d')
    month_start_s = month_start.strftime('%Y-%m-%d')
    fy_start_s  = fy_start.strftime('%Y-%m-%d')

    # ── KPI stats ────────────────────────────────────────────────────────────
    def _sum_qty(from_date, to_date=None):
        if to_date:
            cur.execute(
                "SELECT COALESCE(SUM(quantity),0) AS t FROM lueu_lines "
                "WHERE entry_date >= %s AND entry_date <= %s AND (is_deleted IS NOT TRUE)",
                [from_date, to_date]
            )
        else:
            cur.execute(
                "SELECT COALESCE(SUM(quantity),0) AS t FROM lueu_lines "
                "WHERE entry_date = %s AND (is_deleted IS NOT TRUE)",
                [from_date]
            )
        return float(cur.fetchone()['t'] or 0)

    kpis = {
        'ytd':       round(_sum_qty(fy_start_s, today_s), 2),
        'mtd':       round(_sum_qty(month_start_s, today_s), 2),
        'yesterday': round(_sum_qty(yesterday_s), 2),
        'today':     round(_sum_qty(today_s), 2),
    }

    # ── Active VCNs ──────────────────────────────────────────────────────────
    cur.execute('''
        SELECT
            v.id, v.vcn_doc_num, v.vessel_name, v.doc_status,
            cd.cargo_name,
            COALESCE(cd.bl_quantity, 0) AS bl_quantity,
            COALESCE(cd.quantity_uom, '') AS uom,
            'Import' AS decl_type
        FROM vcn_header v
        JOIN vcn_cargo_declaration cd ON cd.vcn_id = v.id
        WHERE v.doc_status != 'Closed'
        UNION ALL
        SELECT
            v.id, v.vcn_doc_num, v.vessel_name, v.doc_status,
            cd.cargo_name,
            COALESCE(cd.bl_quantity, 0) AS bl_quantity,
            COALESCE(cd.quantity_uom, '') AS uom,
            'Export' AS decl_type
        FROM vcn_header v
        JOIN vcn_export_cargo_declaration cd ON cd.vcn_id = v.id
        WHERE v.doc_status != 'Closed'
        ORDER BY id DESC
    ''')
    vcn_declarations = cur.fetchall()

    # Actual handled per VCN + cargo
    cur.execute('''
        SELECT source_id, COALESCE(cargo_name,'') AS cargo_name,
               COALESCE(SUM(quantity),0) AS actual
        FROM lueu_lines
        WHERE source_type = 'VCN' AND (is_deleted IS NOT TRUE)
        GROUP BY source_id, cargo_name
    ''')
    vcn_actual = {}
    for r in cur.fetchall():
        vcn_actual[(r['source_id'], r['cargo_name'])] = float(r['actual'] or 0)

    vcn_rows = []
    seen_vcn = {}
    for r in vcn_declarations:
        key = (r['id'], r['cargo_name'], r['decl_type'])
        if key in seen_vcn:
            continue
        seen_vcn[key] = True
        bl = float(r['bl_quantity'] or 0)
        actual = vcn_actual.get((r['id'], r['cargo_name'] or ''), 0)
        pct = round((actual / bl * 100) if bl > 0 else 0, 1)
        vcn_rows.append({
            'id': r['id'],
            'doc_num': r['vcn_doc_num'],
            'vessel_name': r['vessel_name'],
            'status': r['doc_status'],
            'cargo_name': r['cargo_name'],
            'bl_quantity': round(bl, 2),
            'actual': round(actual, 2),
            'remaining': round(bl - actual, 2),
            'pct': pct,
            'uom': r['uom'],
            'decl_type': r['decl_type'],
            'exceeded': actual > bl and bl > 0,
        })

    # ── Active MBCs ──────────────────────────────────────────────────────────
    # Basis is the header BL quantity — the tier-1 limit the LUEU save-time
    # validation enforces (per-cargo customer-details limits are tier-2 and shown
    # in the per-cargo BL progress panel, not here).
    cur.execute('''
        SELECT
            m.id, m.doc_num, m.mbc_name, m.doc_status,
            COALESCE(m.cargo_name, '') AS cargo_name,
            COALESCE(m.quantity_uom, '') AS uom,
            COALESCE(m.bl_quantity, 0) AS bl_quantity
        FROM mbc_header m
        WHERE m.doc_status != 'Closed'
        ORDER BY m.id DESC
    ''')
    mbc_declarations = cur.fetchall()

    cur.execute('''
        SELECT source_id, COALESCE(SUM(quantity), 0) AS actual
        FROM lueu_lines
        WHERE source_type = 'MBC' AND (is_deleted IS NOT TRUE)
        GROUP BY source_id
    ''')
    mbc_actual_map = {r['source_id']: float(r['actual'] or 0) for r in cur.fetchall()}

    mbc_rows = []
    for r in mbc_declarations:
        bl = float(r['bl_quantity'] or 0)
        actual = mbc_actual_map.get(r['id'], 0)
        if bl > 0 and actual >= bl:
            continue
        pct = round((actual / bl * 100) if bl > 0 else 0, 1)
        mbc_rows.append({
            'id': r['id'],
            'doc_num': r['doc_num'],
            'mbc_name': r['mbc_name'],
            'status': r['doc_status'],
            'cargo_name': r['cargo_name'],
            'bl_quantity': round(bl, 2),
            'actual': round(actual, 2),
            'remaining': round(bl - actual, 2),
            'pct': pct,
            'uom': r['uom'],
            'exceeded': actual > bl and bl > 0,
        })

    # ── Shift breakdown: Today + Yesterday ───────────────────────────────────
    cur.execute('''
        SELECT
            entry_date,
            shift,
            COALESCE(SUM(quantity), 0) AS total_tonnes,
            ROUND(COALESCE(SUM(
                CASE
                    WHEN from_time IS NOT NULL AND to_time IS NOT NULL
                         AND from_time != '' AND to_time != ''
                    THEN
                        CASE
                            WHEN to_time > from_time
                            THEN (
                                (CAST(SPLIT_PART(to_time,':',1) AS INT)*60 + CAST(SPLIT_PART(to_time,':',2) AS INT))
                              - (CAST(SPLIT_PART(from_time,':',1) AS INT)*60 + CAST(SPLIT_PART(from_time,':',2) AS INT))
                            ) / 60.0
                            ELSE
                            (1440
                              - (CAST(SPLIT_PART(from_time,':',1) AS INT)*60 + CAST(SPLIT_PART(from_time,':',2) AS INT))
                              + (CAST(SPLIT_PART(to_time,':',1) AS INT)*60 + CAST(SPLIT_PART(to_time,':',2) AS INT))
                            ) / 60.0
                        END
                    ELSE 0
                END
            ), 0)::numeric, 2) AS total_hrs
        FROM lueu_lines
        WHERE entry_date IN (%s, %s) AND (is_deleted IS NOT TRUE)
        GROUP BY entry_date, shift
        ORDER BY entry_date, shift
    ''', [today_s, yesterday_s])

    shifts_raw = cur.fetchall()

    shifts = {'today': {}, 'yesterday': {}}
    for r in shifts_raw:
        day = 'today' if r['entry_date'] == today_s else 'yesterday'
        shift = r['shift'] or '?'
        shifts[day][shift] = {
            'tonnes': round(float(r['total_tonnes'] or 0), 2),
            'hrs':    float(r['total_hrs'] or 0),
        }

    # ── Current shift (A=06-14, B=14-22, C=22-06) ───────────────────────────
    now = datetime.now()
    h = now.hour
    if 6 <= h < 14:
        current_shift = 'A'
    elif 14 <= h < 22:
        current_shift = 'B'
    else:
        current_shift = 'C'

    # ── All equipment master list ─────────────────────────────────────────────
    cur.execute('SELECT name FROM equipment ORDER BY name')
    all_equipment = [r['name'] for r in cur.fetchall()]

    # ── Today aggregates per equipment ───────────────────────────────────────
    cur.execute('''
        SELECT
            equipment_name,
            COUNT(*)                         AS entry_count,
            COALESCE(SUM(quantity), 0)       AS today_qty,
            MAX(quantity_uom)                AS uom,
            COUNT(CASE WHEN shift = %s THEN 1 END) AS current_shift_count
        FROM lueu_lines
        WHERE entry_date = %s AND (is_deleted IS NOT TRUE)
          AND equipment_name IS NOT NULL AND equipment_name != ''
        GROUP BY equipment_name
    ''', [current_shift, today_s])
    eq_agg = {r['equipment_name']: dict(r) for r in cur.fetchall()}

    # ── Most recent assignment per equipment today (by highest id) ───────────
    cur.execute('''
        SELECT DISTINCT ON (equipment_name)
            equipment_name,
            source_display,
            barge_name,
            cargo_name,
            shift,
            from_time,
            to_time,
            shift_incharge,
            operator_name,
            delay_name,
            id
        FROM lueu_lines
        WHERE entry_date = %s AND (is_deleted IS NOT TRUE)
          AND equipment_name IS NOT NULL AND equipment_name != ''
        ORDER BY equipment_name, id DESC
    ''', [today_s])
    eq_latest = {r['equipment_name']: dict(r) for r in cur.fetchall()}

    # ── Last entry across all equipment today ────────────────────────────────
    cur.execute('''
        SELECT equipment_name, to_time, created_by, source_display, barge_name, id
        FROM lueu_lines
        WHERE entry_date = %s AND (is_deleted IS NOT TRUE)
          AND to_time IS NOT NULL AND to_time != ''
        ORDER BY id DESC
        LIMIT 1
    ''', [today_s])
    last_row = cur.fetchone()
    last_entry = dict(last_row) if last_row else None

    conn.close()

    # ── Build equipment board ─────────────────────────────────────────────────
    equipment_board = []
    for eq in all_equipment:
        agg = eq_agg.get(eq, {})
        lat = eq_latest.get(eq, {})
        entry_count = int(agg.get('entry_count', 0))
        today_qty   = round(float(agg.get('today_qty', 0)), 2)
        cur_shift_count = int(agg.get('current_shift_count', 0))

        if entry_count == 0:
            status = 'no_data'
        elif cur_shift_count == 0:
            status = 'idle'   # has entries today but none in current shift
        else:
            status = 'active'

        equipment_board.append({
            'name':            eq,
            'status':          status,
            'entry_count':     entry_count,
            'today_qty':       today_qty,
            'uom':             agg.get('uom') or '',
            'source_display':  lat.get('source_display') or '',
            'barge_name':      lat.get('barge_name') or '',
            'cargo_name':      lat.get('cargo_name') or '',
            'last_shift':      lat.get('shift') or '',
            'last_to_time':    lat.get('to_time') or '',
            'shift_incharge':  lat.get('shift_incharge') or '',
            'operator_name':   lat.get('operator_name') or '',
            'delay_name':      lat.get('delay_name') or '',
        })

    return {
        'kpis':            kpis,
        'vcn':             vcn_rows,
        'mbc':             mbc_rows,
        'shifts':          shifts,
        'equipment_board': equipment_board,
        'current_shift':   current_shift,
        'last_entry':      last_entry,
        'as_of':           now.strftime('%d-%b-%Y %H:%M:%S'),
        'today':           today_s,
        'yesterday':       yesterday_s,
    }
