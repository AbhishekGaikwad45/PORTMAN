from database import get_db, get_cursor
from datetime import datetime


def get_next_record_number():
    """Generate next service record number"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(
        "SELECT MAX(CAST(SUBSTR(record_number, 4) AS INTEGER)) FROM service_records WHERE record_number LIKE 'SRV%%'"
    )
    result = cur.fetchone()['max']
    conn.close()
    next_num = (result or 0) + 1
    return f"SRV{next_num:04d}"


def get_service_records(page=1, size=20, source_type=None, service_type_id=None, billed_status=None):
    """Get paginated service records with filters"""
    conn = get_db()
    cur = get_cursor(conn)

    where_parts = []
    params = []

    if source_type:
        where_parts.append("sr.source_type = %s")
        params.append(source_type)
    if service_type_id:
        where_parts.append("sr.service_type_id = %s")
        params.append(service_type_id)
    if billed_status == 'billed':
        where_parts.append("sr.is_billed = 1")
    elif billed_status == 'unbilled':
        where_parts.append("sr.is_billed = 0")

    where_clause = ""
    if where_parts:
        where_clause = "WHERE " + " AND ".join(where_parts)

    cur.execute(f'SELECT COUNT(*) FROM service_records sr {where_clause}', params)
    total = cur.fetchone()['count']

    cur.execute(f'''
        SELECT sr.*, st.service_name, st.service_code
        FROM service_records sr
        LEFT JOIN finance_service_types st ON sr.service_type_id = st.id
        {where_clause}
        ORDER BY sr.id DESC
        LIMIT %s OFFSET %s
    ''', params + [size, (page - 1) * size])
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def get_service_record_by_id(record_id):
    """Get service record header + all field values"""
    conn = get_db()
    cur = get_cursor(conn)

    cur.execute('''
        SELECT sr.*, st.service_name, st.service_code
        FROM service_records sr
        LEFT JOIN finance_service_types st ON sr.service_type_id = st.id
        WHERE sr.id = %s
    ''', [record_id])
    header = cur.fetchone()
    if not header:
        conn.close()
        return None, []

    header = dict(header)

    cur.execute('''
        SELECT srv.*, sfd.field_name, sfd.field_label, sfd.field_type
        FROM service_record_values srv
        JOIN service_field_definitions sfd ON srv.field_definition_id = sfd.id
        WHERE srv.service_record_id = %s
        ORDER BY sfd.display_order, sfd.id
    ''', [record_id])
    values = [dict(r) for r in cur.fetchall()]
    conn.close()

    return header, values


def save_service_record(header_data, field_values):
    """Save service record header + EAV values"""
    conn = get_db()
    cur = get_cursor(conn)

    record_id = header_data.get('id')

    if record_id:
        cur.execute('''
            UPDATE service_records
            SET service_type_id=%s, source_type=%s, source_id=%s, source_display=%s,
                ref_source_type=%s, ref_source_id=%s, ref_source_display=%s,
                record_date=%s, billable_quantity=%s, billable_uom=%s,
                doc_status=%s, remarks=%s
            WHERE id=%s
        ''', [
            header_data.get('service_type_id'),
            header_data.get('source_type'),
            header_data.get('source_id'),
            header_data.get('source_display'),
            header_data.get('ref_source_type') or None,
            header_data.get('ref_source_id') or None,
            header_data.get('ref_source_display') or None,
            header_data.get('record_date'),
            header_data.get('billable_quantity'),
            header_data.get('billable_uom'),
            header_data.get('doc_status', 'Pending'),
            header_data.get('remarks'),
            record_id
        ])

        # Delete existing values and re-insert
        cur.execute('DELETE FROM service_record_values WHERE service_record_id = %s', [record_id])
    else:
        header_data['record_number'] = get_next_record_number()
        cur.execute('''
            INSERT INTO service_records
            (record_number, service_type_id, source_type, source_id, source_display,
             ref_source_type, ref_source_id, ref_source_display,
             record_date, billable_quantity, billable_uom, doc_status,
             created_by, created_date, remarks)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', [
            header_data['record_number'],
            header_data.get('service_type_id'),
            header_data.get('source_type'),
            header_data.get('source_id'),
            header_data.get('source_display'),
            header_data.get('ref_source_type') or None,
            header_data.get('ref_source_id') or None,
            header_data.get('ref_source_display') or None,
            header_data.get('record_date'),
            header_data.get('billable_quantity'),
            header_data.get('billable_uom'),
            header_data.get('doc_status', 'Pending'),
            header_data.get('created_by'),
            datetime.now().strftime('%Y-%m-%d'),
            header_data.get('remarks')
        ])
        record_id = cur.fetchone()['id']

    # Insert field values
    for fv in field_values:
        cur.execute('''
            INSERT INTO service_record_values
            (service_record_id, field_definition_id, field_value)
            VALUES (%s, %s, %s)
        ''', [record_id, fv['field_definition_id'], fv.get('field_value')])

    conn.commit()
    conn.close()
    return record_id, header_data.get('record_number')


def is_billed(record_id):
    """True if record is billed in full or in part (locked from edit/delete)"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''SELECT COALESCE(is_billed, 0) = 1
                        OR COALESCE(billed_quantity, 0) > 0 AS locked
                   FROM service_records WHERE id=%s''', [record_id])
    row = cur.fetchone()
    conn.close()
    return bool(row and row['locked'])


def delete_service_record(record_id):
    """Delete service record and its values (cascade)"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('DELETE FROM service_records WHERE id=%s', (record_id,))
    conn.commit()
    conn.close()


def get_service_types_with_fields():
    """Get all active service types (custom fields load separately if configured)"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT id, service_code, service_name, uom
        FROM finance_service_types
        WHERE is_active = 1
        ORDER BY service_name
    ''')
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_field_definitions(service_type_id):
    """Get field definitions for a service type"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT * FROM service_field_definitions
        WHERE service_type_id = %s AND is_active = 1
        ORDER BY display_order, id
    ''', [service_type_id])
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unbilled_records_for_customer(customer_type, customer_id):
    """Get approved, unbilled service records for a customer/agent (used by billing)"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT sr.*, st.service_name, st.service_code, st.sac_code, st.gl_code,
               st.gst_rate_id
        FROM service_records sr
        JOIN finance_service_types st ON sr.service_type_id = st.id
        WHERE sr.source_type = %s AND sr.source_id = %s
        AND sr.doc_status = 'Approved' AND COALESCE(sr.is_billed, 0) = 0
        AND (COALESCE(sr.billable_quantity, 0) <= 0
             OR COALESCE(sr.billable_quantity, 0) - COALESCE(sr.billed_quantity, 0) > 0)
        ORDER BY sr.id
    ''', [customer_type, customer_id])
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]
