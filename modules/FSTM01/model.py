import psycopg2
from database import get_db, get_cursor
from datetime import datetime


def get_all_service_types():
    """Get all active service types for dropdown"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT id, service_code, service_name, service_category, uom
        FROM finance_service_types
        WHERE is_active = 1
        ORDER BY service_name
    ''')
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_billable_service_types():
    """Get only billable service types"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT id, service_code, service_name, service_category, uom, gl_code, sac_code, gst_rate_id
        FROM finance_service_types
        WHERE is_active = 1 AND is_billable = 1
        ORDER BY service_name
    ''')
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_service_type_data(page=1, size=20):
    """Get paginated service type data — system rows shown but locked in UI"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT COUNT(*) FROM finance_service_types')
    total = cur.fetchone()['count']
    cur.execute('''
        SELECT s.*, g.rate_name as gst_rate_name
        FROM finance_service_types s
        LEFT JOIN gst_rates g ON s.gst_rate_id = g.id
        ORDER BY COALESCE(s.is_system, 0) DESC, s.service_name
        LIMIT %s OFFSET %s
    ''', (size, (page-1)*size))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def save_service_type(data):
    """Save service type record"""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        tds_pct = data.get('tds_percent')
        tds_pct = float(tds_pct) if tds_pct not in (None, '', 'null') else None
        tcs_pct = data.get('tcs_percent')
        tcs_pct = float(tcs_pct) if tcs_pct not in (None, '', 'null') else None

        # Convert empty strings to None for integer fields
        for int_field in ('gst_rate_id', 'is_billable', 'is_active'):
            val = data.get(int_field)
            if val == '' or val is None:
                data[int_field] = None
            else:
                data[int_field] = int(val)

        if data.get('id'):
            cur.execute('''
                UPDATE finance_service_types
                SET service_code=%s, service_name=%s, service_category=%s, gl_code=%s, sac_code=%s,
                    gst_rate_id=%s, uom=%s, is_billable=%s, is_active=%s,
                    sap_gl_account=%s, sap_profit_center=%s, sap_cost_center=%s,
                    sap_igst_gl=%s, sap_cgst_gl=%s, sap_sgst_gl=%s, service_sale_flag=%s,
                    sap_tds_gl=%s, sap_tcs_gl=%s,
                    is_tds=%s, tds_percent=%s, is_tcs=%s, tcs_percent=%s, is_triplicate=%s,
                    requires_hsn_input=%s
                WHERE id=%s
            ''', [
                (data.get('service_code') or '').strip(),
                data.get('service_name'),
                data.get('service_category'),
                data.get('gl_code'),
                data.get('sac_code'),
                data.get('gst_rate_id'),
                data.get('uom'),
                data.get('is_billable', 1),
                data.get('is_active', 1),
                data.get('sap_gl_account'),
                data.get('sap_profit_center'),
                data.get('sap_cost_center'),
                data.get('sap_igst_gl') or None,
                data.get('sap_cgst_gl') or None,
                data.get('sap_sgst_gl') or None,
                data.get('service_sale_flag') or 'S',
                data.get('sap_tds_gl') or None,
                data.get('sap_tcs_gl') or None,
                1 if data.get('is_tds') in (1, '1', 'Yes', True) else 0,
                tds_pct,
                1 if data.get('is_tcs') in (1, '1', 'Yes', True) else 0,
                tcs_pct,
                1 if data.get('is_triplicate') in (1, '1', 'Yes', True) else 0,
                1 if data.get('requires_hsn_input') in (1, '1', 'Yes', True) else 0,
                data['id']
            ])
            row_id = data['id']
        else:
            # service_code is required and must be unique for new rows.
            # The UI marks the field required, but saveAll() posts via fetch(),
            # which bypasses HTML5 validation — so enforce it here too.
            service_code = (data.get('service_code') or '').strip()
            if not service_code:
                raise ValueError('Service code is required')
            data['service_code'] = service_code

            cur.execute('''
                INSERT INTO finance_service_types
                (service_code, service_name, service_category, gl_code, sac_code,
                 gst_rate_id, uom, is_billable, is_active,
                 sap_gl_account, sap_profit_center, sap_cost_center,
                 sap_igst_gl, sap_cgst_gl, sap_sgst_gl, service_sale_flag,
                 sap_tds_gl, sap_tcs_gl,
                 is_tds, tds_percent, is_tcs, tcs_percent, is_triplicate, requires_hsn_input,
                 created_by, created_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            ''', [
                data.get('service_code'),
                data.get('service_name'),
                data.get('service_category'),
                data.get('gl_code'),
                data.get('sac_code'),
                data.get('gst_rate_id'),
                data.get('uom'),
                data.get('is_billable', 1),
                data.get('is_active', 1),
                data.get('sap_gl_account'),
                data.get('sap_profit_center'),
                data.get('sap_cost_center'),
                data.get('sap_igst_gl') or None,
                data.get('sap_cgst_gl') or None,
                data.get('sap_sgst_gl') or None,
                data.get('service_sale_flag') or 'S',
                data.get('sap_tds_gl') or None,
                data.get('sap_tcs_gl') or None,
                1 if data.get('is_tds') in (1, '1', 'Yes', True) else 0,
                tds_pct,
                1 if data.get('is_tcs') in (1, '1', 'Yes', True) else 0,
                tcs_pct,
                1 if data.get('is_triplicate') in (1, '1', 'Yes', True) else 0,
                1 if data.get('requires_hsn_input') in (1, '1', 'Yes', True) else 0,
                data.get('created_by'),
                datetime.now().strftime('%Y-%m-%d')
            ])
            row_id = cur.fetchone()['id']

        conn.commit()
        return row_id
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise ValueError('Service code "%s" already exists' % data.get('service_code', ''))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_service_type(row_id):
    """Delete service type record — system types cannot be deleted"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('DELETE FROM finance_service_types WHERE id=%s AND COALESCE(is_system, 0) = 0', (row_id,))
    conn.commit()
    conn.close()


def get_service_type_by_id(service_id):
    """Get service type details by ID"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM finance_service_types WHERE id = %s', [service_id])
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# ===== FIELD DEFINITION FUNCTIONS =====

def get_field_definitions(service_type_id):
    """Get all field definitions for a service type, ordered by display_order"""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT * FROM service_field_definitions
        WHERE service_type_id = %s
        ORDER BY display_order, id
    ''', [service_type_id])
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_field_definition(data):
    """Save or update a field definition"""
    conn = get_db()
    cur = get_cursor(conn)

    if data.get('id'):
        cur.execute('''
            UPDATE service_field_definitions
            SET field_name=%s, field_label=%s, field_type=%s, field_options=%s,
                calculation_formula=%s, calculation_result_type=%s,
                is_required=%s, is_billable_qty=%s, display_order=%s, is_active=%s
            WHERE id=%s
        ''', [
            data.get('field_name'), data.get('field_label'), data.get('field_type'),
            data.get('field_options'), data.get('calculation_formula'),
            data.get('calculation_result_type'),
            data.get('is_required', 0), data.get('is_billable_qty', 0),
            data.get('display_order', 0), data.get('is_active', 1), data['id']
        ])
        row_id = data['id']
    else:
        cur.execute('''
            INSERT INTO service_field_definitions
            (service_type_id, field_name, field_label, field_type, field_options,
             calculation_formula, calculation_result_type,
             is_required, is_billable_qty, display_order, is_active,
             created_by, created_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', [
            data.get('service_type_id'), data.get('field_name'), data.get('field_label'),
            data.get('field_type'), data.get('field_options'),
            data.get('calculation_formula'), data.get('calculation_result_type'),
            data.get('is_required', 0), data.get('is_billable_qty', 0),
            data.get('display_order', 0), data.get('is_active', 1),
            data.get('created_by'), datetime.now().strftime('%Y-%m-%d')
        ])
        row_id = cur.fetchone()['id']

    # Update has_custom_fields flag on the service type
    cur.execute('''
        UPDATE finance_service_types SET has_custom_fields = 1
        WHERE id = %s
    ''', [data.get('service_type_id')])

    conn.commit()
    conn.close()
    return row_id


def delete_field_definition(field_id):
    """Delete a field definition"""
    conn = get_db()
    cur = get_cursor(conn)

    # Get service_type_id before deleting
    cur.execute('SELECT service_type_id FROM service_field_definitions WHERE id = %s', [field_id])
    row = cur.fetchone()
    service_type_id = row['service_type_id'] if row else None

    cur.execute('DELETE FROM service_field_definitions WHERE id=%s', (field_id,))

    # Check if any fields remain, update has_custom_fields flag
    if service_type_id:
        cur.execute('SELECT COUNT(*) FROM service_field_definitions WHERE service_type_id = %s',
                     [service_type_id])
        count = cur.fetchone()['count']
        if count == 0:
            cur.execute('UPDATE finance_service_types SET has_custom_fields = 0 WHERE id = %s',
                         [service_type_id])

    conn.commit()
    conn.close()
