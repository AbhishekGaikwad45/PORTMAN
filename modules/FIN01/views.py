from flask import render_template, request, redirect, url_for, session, jsonify
from . import bp
from . import model
from database import get_user_permissions, get_db, get_cursor, get_module_config
from mail_service import notify_module_approver, get_module_approver_info, build_approval_mail_html


def _queue_bill_approval_request(bill_id, bill_number, customer_name, total_amount):
    info = get_module_approver_info('FIN01')
    if not info.get('approval_add'):
        return
    bill_url = request.host_url.rstrip('/') + url_for('FIN01.view_bill', bill_id=bill_id)
    notify_module_approver(
        module_code='FIN01',
        ref_id=bill_id,
        subject=f"[Portbird DPPL] Bill {bill_number} — Pending Approval",
        body_html=build_approval_mail_html(
            approver_name=info.get('username'),
            action_label='Pending Approval',
            subtitle='Billing — Approval Required',
            details=[
                ('Bill No',       bill_number or '—'),
                ('Customer',      customer_name or '—'),
                ('Total Amount',  f'₹ {float(total_amount or 0):,.2f}'),
            ],
            action_url=bill_url,
            action_btn_label='Review &amp; Approve Bill',
            submitted_by=session.get('username'),
            badge_color='#d97706',
        ),
    )

def _find_already_billed(lines):
    """Return a label for the first selected line whose source is already
    (fully) billed, else None. Mirrors the billables query server-side so a
    stale page or a double-submit can't bill the same cargo/service twice."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        for line in lines:
            if line.get('line_type') == 'service_record' and line.get('service_record_id'):
                cur.execute('SELECT is_billed FROM service_records WHERE id=%s',
                            [line['service_record_id']])
                r = cur.fetchone()
                if r and r['is_billed']:
                    return line.get('description') or f"service #{line['service_record_id']}"
            cstype, csid = line.get('cargo_source_type'), line.get('cargo_source_id')
            if cstype and csid:
                entry = model._CARGO_TABLES.get(cstype)
                if not entry:
                    continue
                table, qty_col = entry
                cur.execute(f'SELECT COALESCE(billed_quantity,0) AS bq, {qty_col} AS tot '
                            f'FROM {table} WHERE id=%s', [csid])
                r = cur.fetchone()
                if r and model.would_overbill(r['bq'], line.get('quantity'), r['tot']):
                    return line.get('description') or f"{cstype} #{csid}"
        return None
    finally:
        conn.close()


@bp.route('/module/FIN01/')
def index():
    """Main FIN01 index - redirect to bills"""
    return redirect(url_for('FIN01.bills'))


@bp.route('/module/FIN01/invoices')
def legacy_invoices():
    """Legacy invoice list route; moved to FINV01"""
    return redirect(url_for('FINV01.invoices'))


@bp.route('/module/FIN01/invoice/generate')
def legacy_generate_invoice():
    """Legacy invoice generation route; moved to FINV01"""
    return redirect(url_for('FINV01.generate_invoice'))


@bp.route('/module/FIN01/invoice/print/<int:invoice_id>')
def legacy_print_invoice(invoice_id):
    """Legacy invoice print route; moved to FINV01"""
    return redirect(url_for('FINV01.print_invoice', invoice_id=invoice_id))


@bp.route('/module/FIN01/bills')
def bills():
    """List all bills"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    perms = get_user_permissions(session['user_id'], 'FIN01')
    page = int(request.args.get('page', 1))
    status_filter = request.args.get('status')
    data, total = model.get_bill_data(page, status_filter=status_filter)

    config = get_module_config('FIN01')
    is_approver = str(config.get('approver_id', '')) == str(session.get('user_id')) or bool(session.get('is_admin'))

    return render_template('bills.html',
                         data=data,
                         page=page,
                         last_page=(total + 19) // 20,
                         status_filter=status_filter,
                         perms=perms,
                         is_approver=is_approver,
                         username=session.get('username'))


@bp.route('/module/FIN01/bill/<int:bill_id>')
def view_bill(bill_id):
    """View bill details"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    perms = get_user_permissions(session['user_id'], 'FIN01')

    # Get bill header
    bill = model.get_bill_by_id(bill_id)
    if not bill:
        return "Bill not found", 404

    # Get bill lines
    bill_lines = model.get_bill_lines(bill_id)

    config = get_module_config('FIN01')
    user_id = session.get('user_id')
    is_approver = str(config.get('approver_id', '')) == str(user_id) or bool(session.get('is_admin'))

    return render_template('bill_view.html',
                         bill=bill,
                         bill_lines=bill_lines,
                         perms=perms,
                         is_approver=is_approver,
                         username=session.get('username'))


@bp.route('/module/FIN01/bill/generate')
def generate_bill():
    """Generate bill - customer-centric"""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    perms = get_user_permissions(session['user_id'], 'FIN01')

    from datetime import datetime
    current_date = datetime.now().strftime('%Y-%m-%d')

    return render_template('generate_bill.html',
                         current_date=current_date,
                         perms=perms,
                         username=session.get('username'))


def _proof_doc_payload(row, module_code, source_id):
    return {
        'id': row['id'],
        'original_filename': row['original_filename'],
        'uploaded_by': row['uploaded_by'],
        'uploaded_at': str(row['uploaded_at'])[:16],
        'source_module': module_code,
        'source_id': source_id,
        'file_url': f'/api/module/{module_code}/proof_docs/file/{row["id"]}',
    }


def _fetch_source_proof_docs(cur, module_code, source_id):
    if module_code == 'LDUD01':
        cur.execute('''
            SELECT id, original_filename, uploaded_by, uploaded_at
            FROM ldud_proof_documents
            WHERE ldud_id = %s
            ORDER BY uploaded_at
        ''', [source_id])
    elif module_code == 'MBC01':
        cur.execute('''
            SELECT id, original_filename, uploaded_by, uploaded_at
            FROM mbc_proof_documents
            WHERE mbc_id = %s
            ORDER BY uploaded_at
        ''', [source_id])
    else:
        return []
    return [_proof_doc_payload(r, module_code, source_id) for r in cur.fetchall()]


@bp.route('/api/module/FIN01/proof_docs/by_source/<source_module>/<int:source_id>')
def proof_docs_by_source(source_module, source_id):
    """Return proof documents for one LDUD or MBC source."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    module_code = {'LDUD': 'LDUD01', 'LDUD01': 'LDUD01',
                   'MBC': 'MBC01', 'MBC01': 'MBC01'}.get(source_module.upper())
    if not module_code:
        return jsonify({'error': 'Invalid proof document source'}), 400

    conn = get_db()
    cur = get_cursor(conn)
    docs = _fetch_source_proof_docs(cur, module_code, source_id)
    conn.close()
    return jsonify({'docs': docs, 'source_module': module_code, 'source_id': source_id})


@bp.route('/api/module/FIN01/proof_docs/by_bill/<int:bill_id>')
def proof_docs_by_bill(bill_id):
    """Return LDUD and MBC proof documents attached to cargo lines on a bill."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT DISTINCT cargo_source_type, cargo_source_id
        FROM bill_lines
        WHERE bill_id = %s
          AND cargo_source_type IN ('VCN_IMPORT', 'VCN_EXPORT', 'MBC')
          AND cargo_source_id IS NOT NULL
    ''', [bill_id])
    sources = cur.fetchall()

    docs = []
    seen_sources = set()
    seen_docs = set()

    for src in sources:
        module_code = None
        source_id = None

        if src['cargo_source_type'] in ('VCN_IMPORT', 'VCN_EXPORT'):
            table = 'vcn_cargo_declaration' if src['cargo_source_type'] == 'VCN_IMPORT' else 'vcn_export_cargo_declaration'
            cur.execute(f'SELECT vcn_id FROM {table} WHERE id = %s', [src['cargo_source_id']])
            decl = cur.fetchone()
            if not decl:
                continue
            cur.execute('SELECT id FROM ldud_header WHERE vcn_id = %s ORDER BY id DESC LIMIT 1', [decl['vcn_id']])
            source = cur.fetchone()
            if source:
                module_code = 'LDUD01'
                source_id = source['id']

        elif src['cargo_source_type'] == 'MBC':
            cur.execute('SELECT mbc_id FROM mbc_customer_details WHERE id = %s', [src['cargo_source_id']])
            source = cur.fetchone()
            if source:
                module_code = 'MBC01'
                source_id = source['mbc_id']

        source_key = (module_code, source_id)
        if not module_code or not source_id or source_key in seen_sources:
            continue
        seen_sources.add(source_key)

        for doc in _fetch_source_proof_docs(cur, module_code, source_id):
            doc_key = (doc['source_module'], doc['id'])
            if doc_key in seen_docs:
                continue
            seen_docs.add(doc_key)
            docs.append(doc)

    conn.close()
    return jsonify({'docs': docs})


@bp.route('/api/module/FIN01/bill/save', methods=['POST'])
def save_bill():
    """Save bill header"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    perms = get_user_permissions(session['user_id'], 'FIN01')
    if not perms['can_add'] and not perms['can_edit']:
        return jsonify({'success': False, 'error': 'No permission'})

    data = request.json

    # Extract lines from data before saving header (lines belong to bill_lines table, not bill_header)
    lines = data.pop('lines', [])

    # Guard against duplicate billing: reject a NEW bill if any selected source
    # is already billed. Catches "went back and re-billed the same items" and
    # most double-submits (the Generate button is also disabled client-side).
    # ponytail: a truly simultaneous double-POST can still slip through the
    # read-before-write window; atomic FOR UPDATE locking on the declaration
    # rows is the upgrade path if that ever shows up in practice.
    if not data.get('id'):
        dup = _find_already_billed(lines)
        if dup:
            return jsonify({'success': False,
                            'error': f'"{dup}" is already billed. Refresh the page to see '
                                     f'the current billable items before generating a bill.'})

    data['created_by'] = session.get('username')
    data['created_date'] = __import__('datetime').datetime.now().strftime('%Y-%m-%d')

    # Set bill status based on approval config
    config = get_module_config('FIN01')
    user_id = session.get('user_id')
    is_approver = str(config.get('approver_id', '')) == str(user_id)
    is_admin = session.get('is_admin')

    if config.get('approval_add'):
        data['bill_status'] = 'Pending Approval'
    else:
        data['bill_status'] = 'Draft'

    # Get source display name if not provided
    if not data.get('source_display') and data.get('source_type') and data.get('source_id'):
        conn = get_db()
        cur = get_cursor(conn)
        if data['source_type'] == 'VCN':
            cur.execute('SELECT vcn_doc_num FROM vcn_header WHERE id=%s', (data['source_id'],))
            row = cur.fetchone()
            data['source_display'] = row['vcn_doc_num'] if row else ''
        elif data['source_type'] == 'MBC':
            cur.execute('SELECT doc_num FROM mbc_header WHERE id=%s', (data['source_id'],))
            row = cur.fetchone()
            data['source_display'] = row['doc_num'] if row else ''
        conn.close()

    # Extract fields not in bill_header table before saving
    customer_state_code = data.pop('customer_state_code', '') or ''

    row_id, bill_number = model.save_bill_header(data)

    # Save bill lines and calculate totals
    subtotal = 0
    cgst_total = 0
    sgst_total = 0
    igst_total = 0

    customer_gstin = data.get('customer_gstin') or ''

    for line in lines:
        line['bill_id'] = row_id
        line['customer_gstin'] = customer_gstin
        line['customer_state_code'] = customer_state_code
        # Map frontend field names to model field names
        if not line.get('service_name') and line.get('description'):
            line['service_name'] = line['description']
        if not line.get('service_description'):
            line['service_description'] = line.get('description', '')
        model.save_bill_line(line)
        subtotal += float(line.get('line_amount') or 0)
        cgst_total += float(line.get('cgst_amount') or 0)
        sgst_total += float(line.get('sgst_amount') or 0)
        igst_total += float(line.get('igst_amount') or 0)

    # Update bill header with calculated totals + mark source records as billed
    total_amount = subtotal + cgst_total + sgst_total + igst_total
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''UPDATE bill_header
        SET subtotal=%s, cgst_amount=%s, sgst_amount=%s, igst_amount=%s, total_amount=%s
        WHERE id=%s''',
        [subtotal, cgst_total, sgst_total, igst_total, total_amount, row_id])

    # Mark service records as billed
    for line in lines:
        if line.get('line_type') == 'service_record' and line.get('service_record_id'):
            cur.execute('UPDATE service_records SET is_billed=1, bill_id=%s WHERE id=%s',
                        [row_id, line['service_record_id']])

    conn.commit()
    conn.close()

    if data.get('bill_status') == 'Pending Approval':
        _queue_bill_approval_request(row_id, bill_number, data.get('customer_name'), total_amount)

    return jsonify({'success': True, 'id': row_id, 'bill_number': bill_number})


@bp.route('/api/module/FIN01/bill/approve', methods=['POST'])
def approve_bill():
    """Approve a bill - only approver or admin"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    config = get_module_config('FIN01')
    user_id = session.get('user_id')
    is_approver = str(config.get('approver_id', '')) == str(user_id)
    is_admin = session.get('is_admin')

    if not is_approver and not is_admin:
        return jsonify({'success': False, 'error': 'Only approver or admin can approve bills'})

    bill_id = request.json.get('id')
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''UPDATE bill_header
        SET bill_status='Approved', approved_by=%s, approved_date=%s
        WHERE id=%s''',
        [session.get('username'), __import__('datetime').datetime.now().strftime('%Y-%m-%d'), bill_id])
    conn.commit()
    conn.close()

    return jsonify({'success': True})


@bp.route('/api/module/FIN01/bill/submit', methods=['POST'])
def submit_bill():
    """Submit bill for approval"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    bill_id = request.json.get('id')
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT bill_status FROM bill_header WHERE id=%s FOR UPDATE', [bill_id])
    row = cur.fetchone()
    if row and row['bill_status'] == 'Rejected':
        # Rejection released the cargo — re-mark it billed before resubmitting
        model.reapply_bill_sources(cur, bill_id)
    cur.execute('''UPDATE bill_header
        SET bill_status='Pending Approval'
        WHERE id=%s''', [bill_id])
    cur.execute('SELECT bill_number, customer_name, total_amount FROM bill_header WHERE id=%s', [bill_id])
    bill = cur.fetchone()
    conn.commit()
    conn.close()

    if bill:
        _queue_bill_approval_request(
            bill_id,
            bill.get('bill_number'),
            bill.get('customer_name'),
            bill.get('total_amount'),
        )

    return jsonify({'success': True})


@bp.route('/api/module/FIN01/bill/reject', methods=['POST'])
def reject_bill():
    """Reject a bill - only approver or admin"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    config = get_module_config('FIN01')
    user_id = session.get('user_id')
    is_approver = str(config.get('approver_id', '')) == str(user_id)
    is_admin = session.get('is_admin')

    if not is_approver and not is_admin:
        return jsonify({'success': False, 'error': 'Only approver or admin can reject bills'})

    bill_id = request.json.get('id')
    reason = request.json.get('reason', '')
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT bill_status FROM bill_header WHERE id=%s FOR UPDATE', [bill_id])
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Bill not found'})
    if row['bill_status'] != 'Rejected':
        # Release cargo/service billed tracking so the lines are billable again
        model.release_bill_sources(cur, bill_id)
    cur.execute('''UPDATE bill_header
        SET bill_status='Rejected', rejection_reason=%s
        WHERE id=%s''', [reason, bill_id])
    conn.commit()
    conn.close()

    return jsonify({'success': True})


@bp.route('/api/module/FIN01/bill-lines/<int:bill_id>')
def get_bill_lines_api(bill_id):
    """Get bill lines for a specific bill (used in invoice generation page)"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    lines = model.get_bill_lines(bill_id)
    return jsonify({'lines': lines})



@bp.route('/api/module/FIN01/service-types')
def get_service_types():
    """Get all active service types with GST rate details"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT s.id, s.service_name, s.service_code, s.sac_code, s.uom, s.gl_code,
               s.gst_rate_id,
               COALESCE(g.cgst_rate, 0) as cgst_rate,
               COALESCE(g.sgst_rate, 0) as sgst_rate,
               COALESCE(g.igst_rate, 0) as igst_rate,
               g.rate_name as gst_rate_name
        FROM finance_service_types s
        LEFT JOIN gst_rates g ON s.gst_rate_id = g.id
        WHERE s.is_active = 1
        ORDER BY s.service_name
    ''')
    rows = cur.fetchall()
    conn.close()

    return jsonify({'data': [dict(r) for r in rows]})


@bp.route('/api/module/FIN01/port-config')
def get_port_config():
    """Get port GST config (state code, GSTIN) from FIN01 module config"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    config = get_module_config('FIN01')
    return jsonify({
        'port_gst_state_code': config.get('port_gst_state_code', ''),
        'port_gstin': config.get('port_gstin', ''),
        'seller_gstin': config.get('seller_gstin', ''),
        'seller_legal_name': config.get('seller_legal_name', ''),
        'seller_address': config.get('seller_address', ''),
        'seller_location': config.get('seller_location', ''),
        'seller_pincode': config.get('seller_pincode', ''),
        'seller_phone': config.get('seller_phone', ''),
        'seller_email': config.get('seller_email', '')
    })


@bp.route('/api/module/FIN01/customer-agreements/<int:customer_id>')
def get_customer_agreements(customer_id):
    """Get all valid active approved agreements for a customer"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')

    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT id, agreement_code, agreement_name, currency_code, valid_from, valid_to
        FROM customer_agreements
        WHERE customer_id = %s
        AND is_active = 1
        AND agreement_status = 'Approved'
        AND valid_from <= %s
        AND (valid_to IS NULL OR valid_to >= %s)
        ORDER BY valid_from DESC
    ''', [customer_id, today, today])
    rows = cur.fetchall()
    conn.close()

    return jsonify({'data': [dict(r) for r in rows]})


@bp.route('/api/module/FIN01/agreement-rate/<customer_type>/<int:customer_id>/<int:service_type_id>')
def get_agreement_rate(customer_type, customer_id, service_type_id):
    """Get rate from active customer/agent agreement. Optionally filter by agreement_id and cargo_name."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from datetime import datetime

    conn = get_db()
    cur = get_cursor(conn)
    today = datetime.now().strftime('%Y-%m-%d')
    agreement_id = request.args.get('agreement_id')
    cargo_name = request.args.get('cargo_name')

    if agreement_id:
        # Try cargo-specific rate first
        if cargo_name:
            cur.execute('''
                SELECT cal.rate, cal.uom, cal.currency_code,
                       ca.agreement_code, ca.agreement_name, cal.cargo_name
                FROM customer_agreement_lines cal
                INNER JOIN customer_agreements ca ON cal.agreement_id = ca.id
                WHERE ca.id = %s AND cal.service_type_id = %s AND cal.cargo_name = %s
            ''', [agreement_id, service_type_id, cargo_name])
            row = cur.fetchone()
            if row:
                conn.close()
                return jsonify({'success': True, 'data': dict(row)})

        # Fallback to generic (no cargo) rate
        cur.execute('''
            SELECT cal.rate, cal.uom, cal.currency_code,
                   ca.agreement_code, ca.agreement_name, cal.cargo_name
            FROM customer_agreement_lines cal
            INNER JOIN customer_agreements ca ON cal.agreement_id = ca.id
            WHERE ca.id = %s AND cal.service_type_id = %s
              AND (cal.cargo_id IS NULL OR cal.cargo_name IS NULL)
        ''', [agreement_id, service_type_id])
    else:
        # Try cargo-specific rate first
        if cargo_name:
            cur.execute('''
                SELECT cal.rate, cal.uom, cal.currency_code,
                       ca.agreement_code, ca.agreement_name, cal.cargo_name
                FROM customer_agreement_lines cal
                INNER JOIN customer_agreements ca ON cal.agreement_id = ca.id
                WHERE ca.customer_type = %s
                AND ca.customer_id = %s
                AND cal.service_type_id = %s
                AND cal.cargo_name = %s
                AND ca.is_active = 1
                AND ca.agreement_status = 'Approved'
                AND ca.valid_from <= %s
                AND (ca.valid_to IS NULL OR ca.valid_to >= %s)
                ORDER BY ca.valid_from DESC
                LIMIT 1
            ''', [customer_type, customer_id, service_type_id, cargo_name, today, today])
            row = cur.fetchone()
            if row:
                conn.close()
                return jsonify({'success': True, 'data': dict(row)})

        # Fallback to generic rate
        cur.execute('''
            SELECT cal.rate, cal.uom, cal.currency_code,
                   ca.agreement_code, ca.agreement_name, cal.cargo_name
            FROM customer_agreement_lines cal
            INNER JOIN customer_agreements ca ON cal.agreement_id = ca.id
            WHERE ca.customer_type = %s
            AND ca.customer_id = %s
            AND cal.service_type_id = %s
            AND ca.is_active = 1
            AND ca.agreement_status = 'Approved'
            AND ca.valid_from <= %s
            AND (ca.valid_to IS NULL OR ca.valid_to >= %s)
            ORDER BY ca.valid_from DESC
            LIMIT 1
        ''', [customer_type, customer_id, service_type_id, today, today])
    row = cur.fetchone()
    conn.close()

    if row:
        return jsonify({'success': True, 'data': dict(row)})
    else:
        return jsonify({'success': False, 'error': 'No valid agreement found'})


@bp.route('/api/module/FIN01/cargo-rates/<customer_type>/<int:customer_id>/<int:service_type_id>')
def get_cargo_rates(customer_type, customer_id, service_type_id):
    """Get all cargo-specific rates for a service type from the agreement."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from datetime import datetime
    conn = get_db()
    cur = get_cursor(conn)
    today = datetime.now().strftime('%Y-%m-%d')
    agreement_id = request.args.get('agreement_id')

    if agreement_id:
        cur.execute('''
            SELECT cal.rate, cal.uom, cal.currency_code, cal.cargo_id, cal.cargo_name
            FROM customer_agreement_lines cal
            INNER JOIN customer_agreements ca ON cal.agreement_id = ca.id
            WHERE ca.id = %s AND cal.service_type_id = %s AND cal.cargo_name IS NOT NULL
        ''', [agreement_id, service_type_id])
    else:
        cur.execute('''
            SELECT cal.rate, cal.uom, cal.currency_code, cal.cargo_id, cal.cargo_name
            FROM customer_agreement_lines cal
            INNER JOIN customer_agreements ca ON cal.agreement_id = ca.id
            WHERE ca.customer_type = %s
            AND ca.customer_id = %s
            AND cal.service_type_id = %s
            AND cal.cargo_name IS NOT NULL
            AND ca.is_active = 1
            AND ca.agreement_status = 'Approved'
            AND ca.valid_from <= %s
            AND (ca.valid_to IS NULL OR ca.valid_to >= %s)
            ORDER BY ca.valid_from DESC
        ''', [customer_type, customer_id, service_type_id, today, today])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Build a map: cargo_name -> rate
    rate_map = {}
    for r in rows:
        if r['cargo_name'] and r['cargo_name'] not in rate_map:
            rate_map[r['cargo_name']] = r['rate']

    return jsonify({'success': True, 'rates': rate_map})


def _build_agreement_rate_maps(cur, customer_type, customer_id, agreement_id, today):
    """One batched rate lookup for the whole bill. Returns (cargo_rates, generic_rates):
       cargo_rates   = {(service_type_id, cargo_name): rate}
       generic_rates = {service_type_id: rate}   (cargo-agnostic line)
    Mirrors the resolution in get_agreement_rate / get_cargo_rates, but in a
    single query so the client doesn't fetch rates line-by-line."""
    if agreement_id:
        cur.execute('''
            SELECT cal.service_type_id, cal.cargo_name, cal.rate
            FROM customer_agreement_lines cal
            INNER JOIN customer_agreements ca ON cal.agreement_id = ca.id
            WHERE ca.id = %s
        ''', [agreement_id])
    else:
        cur.execute('''
            SELECT cal.service_type_id, cal.cargo_name, cal.rate
            FROM customer_agreement_lines cal
            INNER JOIN customer_agreements ca ON cal.agreement_id = ca.id
            WHERE ca.customer_type = %s AND ca.customer_id = %s
              AND ca.is_active = 1 AND ca.agreement_status = 'Approved'
              AND ca.valid_from <= %s AND (ca.valid_to IS NULL OR ca.valid_to >= %s)
            ORDER BY ca.valid_from DESC
        ''', [customer_type, customer_id, today, today])
    cargo_rates, generic_rates = {}, {}
    for r in cur.fetchall():
        stid = r['service_type_id']
        if r['cargo_name']:
            cargo_rates.setdefault((stid, r['cargo_name']), r['rate'])
        else:
            generic_rates.setdefault(stid, r['rate'])
    return cargo_rates, generic_rates


def _resolve_rate(cargo_rates, generic_rates, service_type_id, cargo_name):
    """Cargo-specific rate first, then the cargo-agnostic generic rate."""
    if service_type_id is None:
        return None
    if cargo_name and (service_type_id, cargo_name) in cargo_rates:
        rate = cargo_rates[(service_type_id, cargo_name)]
    else:
        rate = generic_rates.get(service_type_id)
    return float(rate) if rate is not None else None


@bp.route('/api/module/FIN01/customer-billables/<customer_type>/<int:customer_id>')
def get_customer_billables(customer_type, customer_id):
    """Get all billable items for a customer/agent.
    Returns:
      cargo_handling: cargo declaration rows (VCN import/export + MBC) with LDUD closure status
      other_services: approved unbilled service records for this customer
      billed: already billed bill_lines for reference
    """
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    conn = get_db()
    cur = get_cursor(conn)

    # Look up the selected customer/agent name for cargo filtering
    if customer_type == 'Customer':
        cur.execute("SELECT name FROM vessel_customers WHERE id = %s", [customer_id])
    else:
        cur.execute("SELECT name FROM vessel_agents WHERE id = %s", [customer_id])
    cust_row = cur.fetchone()
    customer_name = cust_row['name'] if cust_row else ''

    # --- A. Cargo Handling: direct from cargo declaration tables ---
    # Service types are HARDCODED by source:
    #   vcn_cargo_declaration        → CHGU01 (Cargo Handling Unloading)
    #   vcn_export_cargo_declaration → CHGL01 (Cargo Handling Loading)
    #   mbc_customer_details         → CHGU01 (Cargo Handling Unloading)

    cur.execute("""
        SELECT id, service_code, service_name, sac_code, uom,
               is_tds, tds_percent, is_tcs, tcs_percent
        FROM finance_service_types
        WHERE service_code IN ('CHGL01', 'CHGU01') AND is_active = 1
    """)
    cargo_st_map = {r['service_code']: dict(r) for r in cur.fetchall()}

    cargo_handling = []

    # A1. VCN Import declarations → CHGU01 (Unloading)
    cur.execute("""
        SELECT cd.id, cd.vcn_id, cd.cargo_name, cd.bl_no, cd.bl_date,
               cd.bl_quantity, cd.quantity_uom,
               COALESCE(cd.is_billed, 0) AS is_billed,
               COALESCE(cd.billed_quantity, 0) AS billed_quantity,
               vh.vcn_doc_num, vh.vessel_name
        FROM vcn_cargo_declaration cd
        JOIN vcn_header vh ON cd.vcn_id = vh.id
        WHERE cd.customer_name = %s
          AND (COALESCE(cd.is_billed, 0) = 0
               OR COALESCE(cd.billed_quantity, 0) < cd.bl_quantity)
        ORDER BY vh.vcn_doc_num DESC, cd.id
    """, [customer_name])
    import_decls = [dict(r) for r in cur.fetchall()]

    # A2. VCN Export declarations → CHGL01 (Loading)
    cur.execute("""
        SELECT cd.id, cd.vcn_id, cd.cargo_name, cd.bl_no, cd.bl_date,
               cd.bl_quantity, cd.quantity_uom,
               COALESCE(cd.is_billed, 0) AS is_billed,
               COALESCE(cd.billed_quantity, 0) AS billed_quantity,
               vh.vcn_doc_num, vh.vessel_name
        FROM vcn_export_cargo_declaration cd
        JOIN vcn_header vh ON cd.vcn_id = vh.id
        WHERE cd.customer_name = %s
          AND (COALESCE(cd.is_billed, 0) = 0
               OR COALESCE(cd.billed_quantity, 0) < cd.bl_quantity)
        ORDER BY vh.vcn_doc_num DESC, cd.id
    """, [customer_name])
    export_decls = [dict(r) for r in cur.fetchall()]

    # A3. MBC customer details → CHGU01 (Unloading)
    cur.execute("""
        SELECT cd.id, cd.mbc_id, cd.cargo_name, cd.bill_of_coastal_goods_no,
               cd.quantity, cd.material_po,
               COALESCE(cd.is_billed, 0) AS is_billed,
               COALESCE(cd.billed_quantity, 0) AS billed_quantity,
               mh.doc_num, mh.doc_date, mh.mbc_name, mh.doc_status AS mbc_status
        FROM mbc_customer_details cd
        JOIN mbc_header mh ON cd.mbc_id = mh.id
        WHERE cd.customer_name = %s
          AND (COALESCE(cd.is_billed, 0) = 0
               OR COALESCE(cd.billed_quantity, 0) < cd.quantity)
        ORDER BY mh.doc_num DESC, cd.id
    """, [customer_name])
    mbc_decls = [dict(r) for r in cur.fetchall()]

    # Batch-fetch LDUD closure status for all VCN sources
    vcn_ids_needed = set(r['vcn_id'] for r in import_decls + export_decls)
    ldud_by_vcn = {}
    for vcn_id in vcn_ids_needed:
        cur.execute("""
            SELECT lh.id AS ldud_id, lh.doc_status, lh.material_po_number, h.vcn_doc_num, h.vessel_name
            FROM ldud_header lh
            JOIN vcn_header h ON lh.vcn_id = h.id
            WHERE lh.vcn_id = %s
            ORDER BY lh.id DESC LIMIT 1
        """, [vcn_id])
        row = cur.fetchone()
        if row:
            ldud_by_vcn[vcn_id] = {
                'ldud_id':            row['ldud_id'],
                'vcn_id':             vcn_id,
                'doc_status':         row['doc_status'] or '',
                'material_po_number': row['material_po_number'] or '',
                'doc_label':          f"{row['vcn_doc_num']} / {row['vessel_name']}"
            }

    def _build_cargo_item(decl, cargo_source_type, svc_code,
                          bl_quantity_field='bl_quantity',
                          source_type='VCN', source_id_field='vcn_id',
                          ldud_info=None, mbc_status=None):
        st = cargo_st_map.get(svc_code, {})
        total_qty   = float(decl.get(bl_quantity_field) or 0)
        billed_qty  = float(decl.get('billed_quantity') or 0)
        billable_qty = max(round(total_qty - billed_qty, 3), 0)
        if ldud_info:
            doc_status  = ldud_info.get('doc_status', '')
            is_billable = doc_status in ('Closed', 'Partial Close')
            doc_label   = ldud_info.get('doc_label', '')
            material_po = ldud_info.get('material_po_number', '')
        else:
            doc_status  = mbc_status or ''
            is_billable = doc_status in ('Approved', 'Closed', 'Partial Close')
            if decl.get('vcn_doc_num'):
                # VCN declaration with no linked LDUD record yet
                doc_label  = f"{decl.get('vcn_doc_num', '')} / {decl.get('vessel_name', '')}"
                doc_status = doc_status or 'No LDUD'
            else:
                doc_label  = f"{decl.get('doc_num', '')} / {decl.get('mbc_name', '')}"
            material_po = decl.get('material_po') or ''
        return {
            'source_type':        source_type,
            'source_id':          decl.get(source_id_field),
            'ldud_id':            ldud_info.get('ldud_id') if ldud_info else None,
            'vcn_id':             ldud_info.get('vcn_id') if ldud_info else None,
            'cargo_source_type':  cargo_source_type,
            'cargo_source_id':    decl['id'],
            'doc_label':          doc_label,
            'doc_status':         doc_status,
            'is_billable':        is_billable,
            'service_code':       svc_code,
            'service_type_id':    st.get('id'),
            'service_name':       st.get('service_name', ''),
            'sac_code':           st.get('sac_code', ''),
            'is_tds':             st.get('is_tds', 0),
            'tds_percent':        float(st.get('tds_percent') or 0),
            'is_tcs':             st.get('is_tcs', 0),
            'tcs_percent':        float(st.get('tcs_percent') or 0),
            'total_quantity':     total_qty,
            'billed_quantity':    billed_qty,
            'billable_quantity':  billable_qty,
            'uom':                decl.get('quantity_uom') or st.get('uom') or 'MT',
            'cargo_name':         decl.get('cargo_name') or '',
            'bl_no':              decl.get('bl_no') or decl.get('bill_of_coastal_goods_no') or '',
            'bl_date':            str(decl.get('bl_date') or decl.get('doc_date') or ''),
            'material_po':        material_po,
            'material_po_options': []
        }

    for r in import_decls:
        cargo_handling.append(_build_cargo_item(
            r, 'VCN_IMPORT', 'CHGU01',
            bl_quantity_field='bl_quantity', source_type='VCN', source_id_field='vcn_id',
            ldud_info=ldud_by_vcn.get(r['vcn_id'])
        ))

    for r in export_decls:
        cargo_handling.append(_build_cargo_item(
            r, 'VCN_EXPORT', 'CHGL01',
            bl_quantity_field='bl_quantity', source_type='VCN', source_id_field='vcn_id',
            ldud_info=ldud_by_vcn.get(r['vcn_id'])
        ))

    for r in mbc_decls:
        cargo_handling.append(_build_cargo_item(
            r, 'MBC', 'CHGU01',
            bl_quantity_field='quantity', source_type='MBC', source_id_field='mbc_id',
            mbc_status=r.get('mbc_status')
        ))

    # --- B. Other Services: approved unbilled service records for this customer ---
    cur.execute("""
        SELECT sr.*, st.service_name, st.service_code, st.sac_code, st.gst_rate_id,
               st.is_tds, st.tds_percent, st.is_tcs, st.tcs_percent
        FROM service_records sr
        JOIN finance_service_types st ON sr.service_type_id = st.id
        WHERE sr.source_type = %s AND sr.source_id = %s
        AND sr.doc_status = 'Approved' AND (sr.is_billed = 0 OR sr.is_billed IS NULL)
        ORDER BY sr.id
    """, [customer_type, customer_id])
    other_services = [dict(r) for r in cur.fetchall()]

    # --- Embed agreement rates (one batched lookup, was N client round-trips) ---
    from datetime import datetime as _dt
    today = _dt.now().strftime('%Y-%m-%d')
    agreement_id = request.args.get('agreement_id')
    cargo_rates, generic_rates = _build_agreement_rate_maps(
        cur, customer_type, customer_id, agreement_id, today)
    for item in cargo_handling:
        item['rate'] = _resolve_rate(cargo_rates, generic_rates,
                                     item.get('service_type_id'), item.get('cargo_name'))
    for rec in other_services:
        rec['rate'] = _resolve_rate(cargo_rates, generic_rates,
                                    rec.get('service_type_id'), None)

    # --- C. Already billed lines for reference ---
    cur.execute("""
        SELECT
            bl.*,
            bh.bill_number,
            bh.bill_date,
            bh.bill_status,
            ca.agreement_code,
            ca.agreement_name,
            NULLIF(
                TRIM(
                    COALESCE(ca.agreement_code, '') ||
                    CASE
                        WHEN COALESCE(ca.agreement_name, '') <> '' THEN ' - ' || ca.agreement_name
                        ELSE ''
                    END
                ),
                ''
            ) AS agreement_display
        FROM bill_lines bl
        JOIN bill_header bh ON bl.bill_id = bh.id
        LEFT JOIN customer_agreements ca ON bh.agreement_id = ca.id
        WHERE bh.customer_type = %s AND bh.customer_id = %s
        ORDER BY bh.id DESC, bl.id
        LIMIT 50
    """, [customer_type, customer_id])
    billed = [dict(r) for r in cur.fetchall()]

    # --- C2. Cutover-marked items: flagged billed via admin tool, no bill record ---
    # These have is_billed=1 but bill_id IS NULL on the source declaration row.
    cutover_rows = []

    cur.execute("""
        SELECT cd.id, cd.cargo_name,
               cd.bl_quantity AS quantity, cd.billed_quantity,
               cd.quantity_uom AS uom,
               'VCN_IMPORT' AS cargo_source_type,
               (SELECT lh.material_po_number FROM ldud_header lh
                 WHERE lh.vcn_id = cd.vcn_id ORDER BY lh.id DESC LIMIT 1) AS material_po,
               (vh.vcn_doc_num || ' – VCN Import') AS service_name
        FROM vcn_cargo_declaration cd
        JOIN vcn_header vh ON cd.vcn_id = vh.id
        WHERE cd.customer_name = %s
          AND COALESCE(cd.is_billed, 0) = 1
          AND cd.bill_id IS NULL
    """, [customer_name])
    for r in cur.fetchall():
        d = dict(r)
        d.update({'is_cutover_billed': True, 'bill_number': None, 'bill_date': None,
                  'bill_status': 'Billed', 'agreement_display': None,
                  'rate': None, 'line_amount': None})
        cutover_rows.append(d)

    cur.execute("""
        SELECT cd.id, cd.cargo_name,
               cd.bl_quantity AS quantity, cd.billed_quantity,
               cd.quantity_uom AS uom,
               'VCN_EXPORT' AS cargo_source_type,
               (SELECT lh.material_po_number FROM ldud_header lh
                 WHERE lh.vcn_id = cd.vcn_id ORDER BY lh.id DESC LIMIT 1) AS material_po,
               (vh.vcn_doc_num || ' – VCN Export') AS service_name
        FROM vcn_export_cargo_declaration cd
        JOIN vcn_header vh ON cd.vcn_id = vh.id
        WHERE cd.customer_name = %s
          AND COALESCE(cd.is_billed, 0) = 1
          AND cd.bill_id IS NULL
    """, [customer_name])
    for r in cur.fetchall():
        d = dict(r)
        d.update({'is_cutover_billed': True, 'bill_number': None, 'bill_date': None,
                  'bill_status': 'Billed', 'agreement_display': None,
                  'rate': None, 'line_amount': None})
        cutover_rows.append(d)

    cur.execute("""
        SELECT cd.id, cd.cargo_name,
               cd.quantity, cd.billed_quantity,
               NULL AS uom,
               'MBC' AS cargo_source_type,
               cd.material_po,
               (mh.doc_num || ' – MBC') AS service_name
        FROM mbc_customer_details cd
        JOIN mbc_header mh ON cd.mbc_id = mh.id
        WHERE cd.customer_name = %s
          AND COALESCE(cd.is_billed, 0) = 1
          AND cd.bill_id IS NULL
    """, [customer_name])
    for r in cur.fetchall():
        d = dict(r)
        d.update({'is_cutover_billed': True, 'bill_number': None, 'bill_date': None,
                  'bill_status': 'Billed', 'agreement_display': None,
                  'rate': None, 'line_amount': None})
        cutover_rows.append(d)

    # Cutover items listed first so they're easy to spot
    billed = cutover_rows + billed

    conn.close()

    return jsonify({
        'cargo_handling': cargo_handling,
        'other_services': other_services,
        'billed': billed
    })


@bp.route('/api/module/FIN01/service-records/<customer_type>/<int:customer_id>')
def get_service_records(customer_type, customer_id):
    """Get approved, unbilled service records for a customer/agent"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    from modules.SRV01 import model as srv_model
    records = srv_model.get_unbilled_records_for_customer(customer_type, customer_id)

    conn = get_db()
    cur = get_cursor(conn)
    for rec in records:
        cur.execute('''
            SELECT sfd.field_label, srv.field_value
            FROM service_record_values srv
            JOIN service_field_definitions sfd ON srv.field_definition_id = sfd.id
            WHERE srv.service_record_id = %s
            ORDER BY sfd.display_order, sfd.id
        ''', [rec['id']])
        rec['field_values'] = [dict(r) for r in cur.fetchall()]
    conn.close()

    return jsonify({'data': records})


# Cargo a customer can actually be billed for, by name. Mirrors the gates in
# get_customer_billables: unbilled (or part-billed) AND the parent document
# open enough to bill. Draft cargo is deliberately excluded — it renders locked
# in the UI, so a customer holding only drafts has nothing to bill today.
_BILLABLE_CARGO_COUNT = '''
    SELECT cd.customer_name AS name, COUNT(*) AS n
    FROM vcn_cargo_declaration cd
    JOIN (SELECT DISTINCT ON (vcn_id) vcn_id, doc_status FROM ldud_header
          ORDER BY vcn_id, id DESC) lh ON lh.vcn_id = cd.vcn_id
    WHERE (COALESCE(cd.is_billed,0) = 0 OR COALESCE(cd.billed_quantity,0) < cd.bl_quantity)
      AND lh.doc_status IN ('Closed', 'Partial Close')
    GROUP BY cd.customer_name
  UNION ALL
    SELECT cd.customer_name, COUNT(*)
    FROM vcn_export_cargo_declaration cd
    JOIN (SELECT DISTINCT ON (vcn_id) vcn_id, doc_status FROM ldud_header
          ORDER BY vcn_id, id DESC) lh ON lh.vcn_id = cd.vcn_id
    WHERE (COALESCE(cd.is_billed,0) = 0 OR COALESCE(cd.billed_quantity,0) < cd.bl_quantity)
      AND lh.doc_status IN ('Closed', 'Partial Close')
    GROUP BY cd.customer_name
  UNION ALL
    SELECT cd.customer_name, COUNT(*)
    FROM mbc_customer_details cd
    JOIN mbc_header mh ON mh.id = cd.mbc_id
    WHERE (COALESCE(cd.is_billed,0) = 0 OR COALESCE(cd.billed_quantity,0) < cd.quantity)
      AND mh.doc_status IN ('Approved', 'Closed', 'Partial Close')
    GROUP BY cd.customer_name
'''


@bp.route('/api/module/FIN01/customers/<path:customer_type>')
def get_customers_for_billing(customer_type):
    """Get customers or agents with billing details.

    ?with_billables=1 adds a billable_count per party, drops parties with none,
    and orders by count desc. Opt-in because the admin cutover picker needs the
    full master list (it works on cargo that is already flagged billed)."""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    if customer_type == 'Customer':
        table, active = 'vessel_customers', ''
    elif customer_type == 'Agent':
        table, active = 'vessel_agents', 'WHERE is_active = 1'
    else:
        return jsonify({'error': 'Invalid customer type'}), 400

    conn = get_db()
    cur = get_cursor(conn)
    cols = ('id, name, gstin, gst_state_code, billing_address, city, pincode, '
            'contact_phone, contact_email')

    if request.args.get('with_billables') == '1':
        # Cargo links to a party by name; service records link by id. Both are
        # counted so the ordering reflects everything the page will list.
        cur.execute(f'''
            WITH cargo AS ({_BILLABLE_CARGO_COUNT}),
            svc AS (
                SELECT source_id AS id, COUNT(*) AS n
                FROM service_records
                WHERE source_type = %s AND doc_status = 'Approved'
                  AND COALESCE(is_billed, 0) = 0
                GROUP BY source_id
            )
            SELECT p.{cols.replace(', ', ', p.')},
                   COALESCE((SELECT SUM(n) FROM cargo c WHERE c.name = p.name), 0)
                 + COALESCE((SELECT n FROM svc s WHERE s.id = p.id), 0) AS billable_count
            FROM {table} p
            {active}
            ORDER BY billable_count DESC, p.name
        ''', [customer_type])
        rows = [dict(r) for r in cur.fetchall() if r['billable_count'] > 0]
    else:
        cur.execute(f'SELECT {cols} FROM {table} {active} ORDER BY name')
        rows = [dict(r) for r in cur.fetchall()]

    conn.close()
    return jsonify({'data': rows})
