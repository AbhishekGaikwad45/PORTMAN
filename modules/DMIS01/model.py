from database import get_db, get_cursor

# ── Masters ──────────────────────────────────────────────────────────────────
def get_equipments():
    """Fetch all equipment names from heme_master or equipment master"""
    conn = get_db()
    cur = get_cursor(conn)
    names = []
    try:
        cur.execute("SELECT number FROM heme_master WHERE is_deleted = FALSE ORDER BY number ASC")
        rows = cur.fetchall()
        for r in rows:
            if r.get('number'):
                names.append(r['number'])
    except Exception:
        pass
    if not names:
        try:
            cur.execute("SELECT id, name FROM equipment ORDER BY name ASC")
            rows = cur.fetchall()
            names = [r['name'] for r in rows if r.get('name')]
        except Exception:
            pass
    conn.close()
    return names


def get_heme_list():
    """Fetch all active HEME records (type, model, number)"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT id, type, model, number, created_by, created_at FROM heme_master WHERE is_deleted = FALSE ORDER BY number ASC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_heme(data):
    """Insert or update a HEME record"""
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')
    eq_type = str(data.get('type') or '').strip()
    model = str(data.get('model') or '').strip()
    number = str(data.get('number') or '').strip()
    created_by = str(data.get('created_by') or 'admin').strip()

    if row_id and str(row_id).isdigit():
        cur.execute("""
            UPDATE heme_master SET type = %s, model = %s, number = %s
            WHERE id = %s RETURNING id
        """, (eq_type, model, number, int(row_id)))
        res = cur.fetchone()
        saved_id = res['id'] if res else row_id
    else:
        cur.execute("""
            INSERT INTO heme_master (type, model, number, created_by)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (eq_type, model, number, created_by))
        res = cur.fetchone()
        saved_id = res['id'] if res else None

    conn.commit()
    conn.close()
    return saved_id


def delete_heme(row_id):
    """Soft delete a HEME record"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("UPDATE heme_master SET is_deleted = TRUE WHERE id = %s", (row_id,))
    conn.commit()
    conn.close()


# ── Tab 1: Diesel Consumption ────────────────────────────────────────────────
def get_consumption(date=None, equipment=None, page=1, size=20, filters=None):
    conn = get_db()
    cur = get_cursor(conn)
    
    where = ["is_deleted = FALSE"]
    params = []
    if date:
        where.append("entry_date = %s")
        params.append(date)
    if equipment:
        where.append("equipment = %s")
        params.append(equipment)

    allowed_fields = {'entry_date', 'equipment', 'start_reading', 'end_reading',
                      'difference', 'diesel_filled', 'rate', 'remarks', 'created_by'}
    for f in (filters or []):
        field = f.get('field', '')
        if field not in allowed_fields:
            continue
        ftype = f.get('type')
        if ftype == 'contains' and f.get('value'):
            where.append(f"{field} ILIKE %s")
            params.append(f"%{f['value']}%")
        elif ftype == 'multi' and f.get('values'):
            ph = ','.join(['%s'] * len(f['values']))
            where.append(f"{field} IN ({ph})")
            params.extend(f['values'])
        elif ftype == 'range':
            if f.get('from'):
                where.append(f"{field} >= %s")
                params.append(f'{f["from"]}')
            if f.get('to'):
                where.append(f"{field} <= %s")
                params.append(f'{f["to"]}')
        
    where_clause = " WHERE " + " AND ".join(where)
    
    cur.execute(f"SELECT COUNT(*) as count FROM dmis01_diesel_consumption {where_clause}", params)
    total = cur.fetchone()['count']
    
    if page and size and size > 0:
        offset = (page - 1) * size
        cur.execute(f"""
            SELECT * FROM dmis01_diesel_consumption 
            {where_clause} 
            ORDER BY entry_date DESC, id DESC 
            LIMIT %s OFFSET %s
        """, (*params, size, offset))
    else:
        cur.execute(f"""
            SELECT * FROM dmis01_diesel_consumption 
            {where_clause} 
            ORDER BY entry_date DESC, id DESC
        """, params)
        
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def get_latest_end_reading(equipment):
    """Fetch the latest active end reading for smart auto carry-forward"""
    if not equipment:
        return '0'
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT end_reading 
        FROM dmis01_diesel_consumption 
        WHERE equipment = %s AND is_deleted = FALSE 
        ORDER BY entry_date DESC, id DESC 
        LIMIT 1
    """, (equipment,))
    row = cur.fetchone()
    conn.close()
    return row['end_reading'] if row and row.get('end_reading') else '0'


def save_consumption(data):
    import re
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')
    
    entry_date = str(data.get('entry_date') or '').strip()
    equipment = str(data.get('equipment') or '').strip()
    start_reading = str(data.get('start_reading') if data.get('start_reading') is not None else '').strip()
    end_reading = str(data.get('end_reading') if data.get('end_reading') is not None else '').strip()
    difference = str(data.get('difference') if data.get('difference') is not None else '').strip()
    diesel_filled = str(data.get('diesel_filled') if data.get('diesel_filled') is not None else '').strip()
    diesel_filled = re.sub(r'[^0-9.]', '', diesel_filled)
    rate = str(data.get('rate') if data.get('rate') is not None else '').strip()
    rate = re.sub(r'[^0-9.]', '', rate)
    if not diesel_filled or float(diesel_filled or 0) == 0:
        rate = ''
    remarks = str(data.get('remarks') or '').strip()
    created_by = str(data.get('created_by') or 'admin').strip()
        
    if row_id and str(row_id).isdigit():
        cur.execute("""
            UPDATE dmis01_diesel_consumption SET
                entry_date = %s, equipment = %s,
                start_reading = %s, end_reading = %s, difference = %s,
                diesel_filled = %s, rate = %s, remarks = %s
            WHERE id = %s RETURNING id
        """, (entry_date, equipment, start_reading, end_reading, difference, diesel_filled, rate, remarks, int(row_id)))
        res = cur.fetchone()
        saved_id = res['id'] if res else row_id
    else:
        cur.execute("""
            INSERT INTO dmis01_diesel_consumption (
                entry_date, equipment, start_reading, end_reading,
                difference, diesel_filled, rate, remarks, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (entry_date, equipment, start_reading, end_reading, difference, diesel_filled, rate, remarks, created_by))
        res = cur.fetchone()
        saved_id = res['id'] if res else None

    conn.commit()
    conn.close()
    return saved_id

    conn.commit()
    conn.close()
    return saved_id


def delete_consumption(row_id):
    """Soft delete a consumption record"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("UPDATE dmis01_diesel_consumption SET is_deleted = TRUE WHERE id = %s", (row_id,))
    conn.commit()
    conn.close()


# ── Tab 2: Diesel Received ───────────────────────────────────────────────────
def get_received(month=None, page=1, size=20, filters=None):
    conn = get_db()
    cur = get_cursor(conn)
    
    where = ["is_deleted = FALSE"]
    params = []
    if month:
        where.append("received_date LIKE %s")
        params.append(f"{month}%")

    allowed_fields = {'received_date', 'diesel_received', 'created_by'}
    for f in (filters or []):
        field = f.get('field', '')
        if field not in allowed_fields:
            continue
        ftype = f.get('type')
        if ftype == 'contains' and f.get('value'):
            where.append(f"{field} ILIKE %s")
            params.append(f"%{f['value']}%")
        elif ftype == 'range':
            if f.get('from'):
                where.append(f"{field} >= %s")
                params.append(f'{f["from"]}')
            if f.get('to'):
                where.append(f"{field} <= %s")
                params.append(f'{f["to"]}')
        
    where_clause = " WHERE " + " AND ".join(where)
    
    cur.execute(f"SELECT COUNT(*) as count FROM dmis01_diesel_received {where_clause}", params)
    total = cur.fetchone()['count']
    
    if page and size and size > 0:
        offset = (page - 1) * size
        cur.execute(f"""
            SELECT * FROM dmis01_diesel_received 
            {where_clause} 
            ORDER BY received_date DESC, id DESC 
            LIMIT %s OFFSET %s
        """, (*params, size, offset))
    else:
        cur.execute(f"""
            SELECT * FROM dmis01_diesel_received 
            {where_clause} 
            ORDER BY received_date DESC, id DESC
        """, params)
        
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def save_received(data):
    import re
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')
    
    received_date = str(data.get('received_date') or '').strip()
    diesel_received = str(data.get('diesel_received') if data.get('diesel_received') is not None else '').strip()
    diesel_received = re.sub(r'[^0-9.]', '', diesel_received)
    created_by = str(data.get('created_by') or 'admin').strip()
    
    if row_id and str(row_id).isdigit():
        cur.execute("""
            UPDATE dmis01_diesel_received SET
                received_date = %s, diesel_received = %s
            WHERE id = %s RETURNING id
        """, (received_date, diesel_received, int(row_id)))
        res = cur.fetchone()
        saved_id = res['id'] if res else row_id
    else:
        cur.execute("""
            INSERT INTO dmis01_diesel_received (received_date, diesel_received, created_by)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (received_date, diesel_received, created_by))
        res = cur.fetchone()
        saved_id = res['id'] if res else None

    conn.commit()
    conn.close()
    return saved_id


def delete_received(row_id):
    """Soft delete a diesel received record"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("UPDATE dmis01_diesel_received SET is_deleted = TRUE WHERE id = %s", (row_id,))
    conn.commit()
    conn.close()


# ── Tab 3: Tank Stock ────────────────────────────────────────────────────────
def get_tank_stock(date=None, equipment=None, page=1, size=20, filters=None):
    conn = get_db()
    cur = get_cursor(conn)
    
    where = ["is_deleted = FALSE"]
    params = []
    if date:
        where.append("stock_date = %s")
        params.append(date)
    if equipment:
        where.append("equipment = %s")
        params.append(equipment)

    allowed_fields = {'stock_date', 'equipment', 'month_start_tank_stock', 'created_by'}
    for f in (filters or []):
        field = f.get('field', '')
        if field not in allowed_fields:
            continue
        ftype = f.get('type')
        if ftype == 'contains' and f.get('value'):
            where.append(f"{field} ILIKE %s")
            params.append(f"%{f['value']}%")
        elif ftype == 'multi' and f.get('values'):
            ph = ','.join(['%s'] * len(f['values']))
            where.append(f"{field} IN ({ph})")
            params.extend(f['values'])
        elif ftype == 'range':
            if f.get('from'):
                where.append(f"{field} >= %s")
                params.append(f'{f["from"]}')
            if f.get('to'):
                where.append(f"{field} <= %s")
                params.append(f'{f["to"]}')
        
    where_clause = " WHERE " + " AND ".join(where)
    
    cur.execute(f"SELECT COUNT(*) as count FROM dmis01_tank_stock {where_clause}", params)
    total = cur.fetchone()['count']
    
    if page and size and size > 0:
        offset = (page - 1) * size
        cur.execute(f"""
            SELECT * FROM dmis01_tank_stock 
            {where_clause} 
            ORDER BY stock_date DESC, equipment ASC 
            LIMIT %s OFFSET %s
        """, (*params, size, offset))
    else:
        cur.execute(f"""
            SELECT * FROM dmis01_tank_stock 
            {where_clause} 
            ORDER BY stock_date DESC, equipment ASC
        """, params)
        
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def save_tank_stock(data):
    import re
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')
    
    stock_date = str(data.get('stock_date') or data.get('stock_month') or '').strip()
    equipment = str(data.get('equipment') or '').strip()
    month_start_tank_stock = str(data.get('month_start_tank_stock') if data.get('month_start_tank_stock') is not None else '').strip()
    month_start_tank_stock = re.sub(r'[^0-9.]', '', month_start_tank_stock)
    created_by = str(data.get('created_by') or 'admin').strip()
    
    if row_id and str(row_id).isdigit():
        cur.execute("""
            UPDATE dmis01_tank_stock SET
                stock_date = %s, equipment = %s, month_start_tank_stock = %s
            WHERE id = %s RETURNING id
        """, (stock_date, equipment, month_start_tank_stock, int(row_id)))
        res = cur.fetchone()
        saved_id = res['id'] if res else row_id
    else:
        cur.execute("""
            INSERT INTO dmis01_tank_stock (stock_date, equipment, month_start_tank_stock, created_by)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (stock_date, equipment) DO UPDATE SET
                month_start_tank_stock = EXCLUDED.month_start_tank_stock,
                is_deleted = FALSE
            RETURNING id
        """, (stock_date, equipment, month_start_tank_stock, created_by))
        res = cur.fetchone()
        saved_id = res['id'] if res else None

    conn.commit()
    conn.close()
    return saved_id

    conn.commit()
    conn.close()
    return saved_id


def delete_tank_stock(row_id):
    """Soft delete a tank stock record"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("UPDATE dmis01_tank_stock SET is_deleted = TRUE WHERE id = %s", (row_id,))
    conn.commit()
    conn.close()


# ── MIS Summary & Reconciliation ─────────────────────────────────────────────
def get_mis_summary(month=None):
    """Aggregate consumption and inward receipts for summary KPIs"""
    conn = get_db()
    cur = get_cursor(conn)
    
    # 1. Consumption per equipment
    c_where = "WHERE is_deleted = FALSE"
    c_params = []
    if month:
        c_where += " AND entry_date LIKE %s"
        c_params.append(f"{month}%")
        
    cur.execute(f"""
        SELECT 
            equipment,
            COUNT(*) as entries_count,
            SUM(NULLIF(difference, '')::numeric) as total_difference,
            SUM(NULLIF(diesel_filled, '')::numeric) as total_diesel_filled
        FROM dmis01_diesel_consumption
        {c_where}
        GROUP BY equipment
        ORDER BY equipment ASC
    """, c_params)
    equip_rows = [dict(r) for r in cur.fetchall()]
    
    # 2. Total Inward Diesel Received
    r_where = "WHERE is_deleted = FALSE"
    r_params = []
    if month:
        r_where += " AND received_date LIKE %s"
        r_params.append(f"{month}%")
    cur.execute(f"""
        SELECT SUM(NULLIF(diesel_received, '')::numeric) as total_received
        FROM dmis01_diesel_received
        {r_where}
    """, r_params)
    total_received_row = cur.fetchone()
    total_received = float(total_received_row['total_received'] or 0) if total_received_row else 0.0

    conn.close()
    return {
        'equipment_summary': equip_rows,
        'total_diesel_received': total_received
    }
