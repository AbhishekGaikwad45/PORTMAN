from database import get_db, get_cursor

def get_anchorage(mbc_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM mbct01_anchorage WHERE mbc_id=%s ORDER BY id', (mbc_id,))
    return cur.fetchall()

def save_anchorage(data):
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')
    cols = ['eta_gull', 'nor_tendered_green_buoy', 'anchor_dropped_gull', 'anchor_away_gull', 
            'passing_gull_island', 'passing_high_tension', 'passing_yellow_crane', 'remarks']
    vals = [data.get(c) for c in cols]

    if row_id:
        set_clause = ', '.join([f"{c}=%s" for c in cols])
        cur.execute(f"UPDATE mbct01_anchorage SET {set_clause} WHERE id=%s RETURNING id", (*vals, row_id))
    else:
        cols_str = ', '.join(cols + ['mbc_id'])
        placeholders = ', '.join(['%s'] * (len(cols) + 1))
        cur.execute(f"INSERT INTO mbct01_anchorage ({cols_str}) VALUES ({placeholders}) RETURNING id", (*vals, data['mbc_id']))
    
    res = cur.fetchone()
    conn.commit()
    return res['id'] if res else None

def delete_anchorage(row_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('DELETE FROM mbct01_anchorage WHERE id=%s', (row_id,))
    conn.commit()


def get_port_activity(mbc_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM mbct01_port_activity WHERE mbc_id=%s ORDER BY id', (mbc_id,))
    return cur.fetchall()

def save_port_activity(data):
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')
    cols = ['activity_id', 'tugs', 'first_line_ashore_cast_off', 'all_made_fast_cast_off', 'shore_supply_starts', 
            'shore_supply_ends', 'from_berth_anchorage_id', 'to_berth_anchorage_id', 'loaded_condition', 
            'mbc_position', 'reasons']
    vals = [data.get(c) for c in cols]

    if row_id:
        set_clause = ', '.join([f"{c}=%s" for c in cols])
        cur.execute(f"UPDATE mbct01_port_activity SET {set_clause} WHERE id=%s RETURNING id", (*vals, row_id))
    else:
        cols_str = ', '.join(cols + ['mbc_id'])
        placeholders = ', '.join(['%s'] * (len(cols) + 1))
        cur.execute(f"INSERT INTO mbct01_port_activity ({cols_str}) VALUES ({placeholders}) RETURNING id", (*vals, data['mbc_id']))
    
    res = cur.fetchone()
    conn.commit()
    return res['id'] if res else None

def delete_port_activity(row_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('DELETE FROM mbct01_port_activity WHERE id=%s', (row_id,))
    conn.commit()


def get_tug_activity(mbc_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT t.*, p.activity_id as port_activity_name, p.id as port_activity_id
        FROM mbct01_tug_activity t
        JOIN mbct01_port_activity p ON t.port_activity_id = p.id
        WHERE p.mbc_id = %s
        ORDER BY p.id, t.id
    ''', (mbc_id,))
    return cur.fetchall()

def save_tug_activity(data):
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')
    
    p_id = data.get('port_activity_id')
    mbc_id = data.get('mbc_id')
    tug_id = data.get('tug_id')
    
    if not tug_id:
        return None
        
    actual_p_id = None
    if isinstance(p_id, int) or (isinstance(p_id, str) and str(p_id).isdigit()):
        actual_p_id = int(p_id)
    else:
        activity_str = str(p_id or data.get('activity_id') or '')
        cur.execute("SELECT id FROM mbct01_port_activity WHERE mbc_id=%s AND activity_id=%s LIMIT 1", (mbc_id, activity_str))
        p_row = cur.fetchone()
        if p_row:
            actual_p_id = p_row['id']
            
    if not actual_p_id:
        activity_str = str(p_id or data.get('activity_id') or '')
        cur.execute("INSERT INTO mbct01_port_activity (mbc_id, activity_id) VALUES (%s, %s) RETURNING id", (mbc_id, activity_str))
        actual_p_id = cur.fetchone()['id']

    # Sanitize numeric fields
    t_running_hours = data.get('t_running_hours')
    if t_running_hours == '' or t_running_hours is None:
        t_running_hours = None

    fuel_used = data.get('fuel_used')
    if fuel_used == '' or fuel_used is None:
        fuel_used = None

    cur.execute("""
        INSERT INTO mbct01_tug_activity 
            (port_activity_id, tug_id, tug_made_fast, tug_cast_off, engine_start, engine_stop, t_running_hours, fuel_used, remark)
        VALUES 
            (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (port_activity_id, tug_id) DO UPDATE SET
            tug_made_fast = EXCLUDED.tug_made_fast,
            tug_cast_off = EXCLUDED.tug_cast_off,
            engine_start = EXCLUDED.engine_start,
            engine_stop = EXCLUDED.engine_stop,
            t_running_hours = EXCLUDED.t_running_hours,
            fuel_used = EXCLUDED.fuel_used,
            remark = EXCLUDED.remark
        RETURNING id
    """, (
        actual_p_id,
        tug_id,
        data.get('tug_made_fast') or None,
        data.get('tug_cast_off') or None,
        data.get('engine_start') or None,
        data.get('engine_stop') or None,
        t_running_hours,
        fuel_used,
        data.get('remark') or None
    ))
    res = cur.fetchone()
    conn.commit()
    return res['id'] if res else None

def save_tug_activity_batch(data_list):
    saved_ids = []
    for item in data_list:
        rid = save_tug_activity(item)
        if rid:
            saved_ids.append(rid)
    return saved_ids

def delete_tug_activity(port_activity_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('DELETE FROM mbct01_tug_activity WHERE port_activity_id=%s', (port_activity_id,))
    conn.commit()
