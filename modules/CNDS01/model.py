from database import get_db, get_cursor

TABLE = 'fdcn_doc_series'

# DN and CN each carry their own default, so one series per type can be active
# at a time — mirrors how the old FDCN01 doc-series page keyed its lookup.
DOC_TYPES = ('DN', 'CN')


def ensure_table(cur):
    """Create the series table on first use, same as invoice_doc_series."""
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            prefix TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'CN',
            is_default BOOLEAN DEFAULT FALSE
        )
    ''')


def get_data(page=1, size=20):
    conn = get_db()
    cur = get_cursor(conn)
    ensure_table(cur)
    conn.commit()
    cur.execute(f'SELECT COUNT(*) FROM {TABLE}')
    total = cur.fetchone()['count']
    cur.execute(f'SELECT * FROM {TABLE} ORDER BY type, name LIMIT %s OFFSET %s',
                (size, (page - 1) * size))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def get_all():
    conn = get_db()
    cur = get_cursor(conn)
    ensure_table(cur)
    conn.commit()
    cur.execute(f'SELECT id, name, prefix, type, is_default FROM {TABLE} ORDER BY type, name')
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_data(data):
    conn = get_db()
    cur = get_cursor(conn)
    ensure_table(cur)
    from modules.FDCN01.model import normalize_prefix
    row_id = data.get('id')
    name = data.get('name', '')
    # Stored the way FDCN01 will read it, so a stray trailing '/' can't produce
    # a doubled separator in the issued doc number.
    prefix = normalize_prefix(data.get('prefix'))
    doc_type = (data.get('type') or 'CN').upper().strip()
    if doc_type not in DOC_TYPES:
        doc_type = 'CN'
    is_default = bool(data.get('is_default', False))

    # Clearing the old default is scoped to this type — setting a CN default
    # must not strip the DN one.
    if is_default:
        cur.execute(f'UPDATE {TABLE} SET is_default = FALSE WHERE type = %s AND is_default = TRUE',
                    [doc_type])

    if row_id:
        cur.execute(f'UPDATE {TABLE} SET name=%s, prefix=%s, type=%s, is_default=%s WHERE id=%s',
                    [name, prefix, doc_type, is_default, row_id])
    else:
        cur.execute(f'INSERT INTO {TABLE} (name, prefix, type, is_default) '
                    f'VALUES (%s, %s, %s, %s) RETURNING id',
                    [name, prefix, doc_type, is_default])
        row_id = cur.fetchone()['id']

    conn.commit()
    conn.close()
    return row_id


def delete_data(row_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(f'DELETE FROM {TABLE} WHERE id=%s', (row_id,))
    conn.commit()
    conn.close()
