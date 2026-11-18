from database import get_db, get_cursor

TABLE = 'heme_master'


def get_all():
    """Fetch all active HEME records ordered by number"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(f"SELECT id, type, model, number FROM {TABLE} WHERE is_deleted = FALSE ORDER BY number ASC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_data(page=1, size=20, search=''):
    """Fetch paginated HEME records with total count"""
    conn = get_db()
    cur = get_cursor(conn)
    where = ["is_deleted = FALSE"]
    params = []

    if search:
        where.append("(type ILIKE %s OR model ILIKE %s OR number ILIKE %s)")
        s = f"%{search}%"
        params.extend([s, s, s])

    where_sql = " WHERE " + " AND ".join(where)

    cur.execute(f"SELECT COUNT(*) as count FROM {TABLE} {where_sql}", params)
    total = cur.fetchone()['count']

    if size and size > 0:
        offset = (page - 1) * size
        cur.execute(f"""
            SELECT * FROM {TABLE} 
            {where_sql} 
            ORDER BY number ASC, id DESC 
            LIMIT %s OFFSET %s
        """, (*params, size, offset))
    else:
        cur.execute(f"""
            SELECT * FROM {TABLE} 
            {where_sql} 
            ORDER BY number ASC, id DESC
        """, params)

    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def save_data(data):
    """Insert or update a HEME record"""
    conn = get_db()
    cur = get_cursor(conn)
    row_id = data.get('id')

    eq_type = str(data.get('type') or '').strip()
    model = str(data.get('model') or '').strip()
    number = str(data.get('number') or '').strip()
    created_by = str(data.get('created_by') or 'admin').strip()

    if row_id and str(row_id).isdigit():
        cur.execute(f"""
            UPDATE {TABLE} SET type=%s, model=%s, number=%s
            WHERE id=%s RETURNING id
        """, (eq_type, model, number, int(row_id)))
        res = cur.fetchone()
        saved_id = res['id'] if res else row_id
    else:
        cur.execute(f"""
            INSERT INTO {TABLE} (type, model, number, created_by)
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (eq_type, model, number, created_by))
        res = cur.fetchone()
        saved_id = res['id'] if res else None

    conn.commit()
    conn.close()
    return saved_id


def delete_data(row_id):
    """Soft delete a HEME record"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(f"UPDATE {TABLE} SET is_deleted = TRUE WHERE id=%s", (row_id,))
    conn.commit()
    conn.close()
