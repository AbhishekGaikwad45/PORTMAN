import json
from database import get_db, get_cursor

def _to_float_or_none(v):
    try:
        return float(v) if v not in (None, '', 'null') else None
    except (TypeError, ValueError):
        return None

def get_all():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM port_berth_master ORDER BY berth_sequence NULLS LAST, berth_name')
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def save(data):
    conn = get_db()
    cur = get_cursor(conn)
    lat = _to_float_or_none(data.get('latitude'))
    lon = _to_float_or_none(data.get('longitude'))
    seq = data.get('berth_sequence')
    try:
        seq = int(seq) if seq not in (None, '') else None
    except (TypeError, ValueError):
        seq = None

    image_position = data.get('image_position')
    image_position_json = json.dumps(image_position) if image_position else None

    if data.get('id'):
        cur.execute(
            'UPDATE port_berth_master SET berth_name=%s, berth_location=%s, remarks=%s, '
            'latitude=%s, longitude=%s, berth_sequence=%s, image_position=%s::jsonb WHERE id=%s',
            [data.get('berth_name'), data.get('berth_location'), data.get('remarks'),
             lat, lon, seq, image_position_json, data['id']]
        )
        row_id = data['id']
    else:
        cur.execute(
            'INSERT INTO port_berth_master (berth_name, berth_location, remarks, latitude, longitude, berth_sequence, image_position) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id',
            [data.get('berth_name'), data.get('berth_location'), data.get('remarks'), lat, lon, seq, image_position_json]
        )
        row_id = cur.fetchone()['id']
    conn.commit()
    conn.close()
    return row_id

def delete(row_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('DELETE FROM port_berth_master WHERE id=%s', [row_id])
    conn.commit()
    conn.close()
