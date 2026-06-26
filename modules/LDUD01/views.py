import io
import json as _json
import mimetypes
import os
import psycopg2
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, send_file
from werkzeug.security import check_password_hash
from functools import wraps
from . import model
from database import get_user_permissions, get_module_config, get_db, get_cursor
from mail_service import (
    queue_mail as _queue_mail,
    trigger_mail_processing as _trigger_mail_processing,
    build_approval_mail_html as _build_approval_mail_html,
)

bp = Blueprint('LDUD01', __name__, template_folder='.')
MODULE_CODE = 'LDUD01'

def _get_user_email_by_id(user_id):
    """Return (email, username) for a user_id."""
    if not user_id:
        return None, None
    from database import get_db, get_cursor
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT email, username FROM users WHERE id=%s', [user_id])
    row = cur.fetchone()
    conn.close()
    return (row['email'], row['username']) if row else (None, None)

def _get_closer_email(record_id):
    """Return (email, username) of the last user who closed this LDUD record."""
    from database import get_db, get_cursor
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT actioned_by FROM approval_log
        WHERE module_code='LDUD01' AND record_id=%s
          AND action IN ('Closed','Partial Close')
        ORDER BY actioned_at DESC LIMIT 1
    """, [record_id])
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, None
    closer_username = row['actioned_by']
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT email, username FROM users WHERE username=%s', [closer_username])
    row2 = cur.fetchone()
    conn.close()
    return (row2['email'], row2['username']) if row2 else (None, closer_username)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_perms():
    if session.get('is_admin'):
        return {'can_read': 1, 'can_add': 1, 'can_edit': 1, 'can_delete': 1}
    return get_user_permissions(session.get('user_id'), MODULE_CODE)

@bp.route('/module/LDUD01/')
@login_required
def view():
    perms = get_perms()
    if not perms.get('can_read'):
        return render_template('no_access.html'), 403
    return render_template('ldud01.html', permissions=perms)

@bp.route('/api/module/LDUD01/data')
@login_required
def get_data():
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 20))
    except (ValueError, TypeError):
        page, size = 1, 20
    try:
        filters = _json.loads(request.args.get('filters', '[]'))
    except _json.JSONDecodeError:
        filters = []
    rows, total = model.get_data(page, size, filters)
    return jsonify({'data': rows, 'last_page': (total + size - 1) // size, 'total': total})

@bp.route('/api/module/LDUD01/vcn_list')
@login_required
def get_vcn_list():
    return jsonify(model.get_vcn_list())

@bp.route('/api/module/LDUD01/vcn_list/export')
@login_required
def get_export_vcn_list():
    return jsonify(model.get_vcn_list())

@bp.route('/api/module/LDUD01/save', methods=['POST'])
@login_required
def save():
    perms = get_perms()
    data = request.json
    is_new = not data.get('id')
    if is_new and not perms.get('can_add'):
        return jsonify({'error': 'No permission to add'}), 403
    if not is_new and not perms.get('can_edit'):
        return jsonify({'error': 'No permission to edit'}), 403

    config = get_module_config('LDUD01')
    is_approver = str(config.get('approver_id', '')) == str(session.get('user_id')) or session.get('is_admin')

    if not is_new:
        current_status = model.get_doc_status(data['id'])
        if current_status == 'Closed':
            if not is_approver:
                return jsonify({'error': 'Cannot edit a closed record'}), 403
            data['doc_status'] = 'Closed'
        elif current_status == 'Partial Close':
            data['doc_status'] = 'Partial Close'
        else:
            data['doc_status'] = 'Draft'
    else:
        data['doc_status'] = 'Draft'

    row_id, doc_num = model.save_header(data)
    return jsonify({'id': row_id, 'doc_num': doc_num, 'doc_status': data.get('doc_status', 'Draft')})


@bp.route('/api/module/LDUD01/closure_check/<int:ldud_id>')
@login_required
def closure_check(ldud_id):
    return jsonify(model.get_closure_eligibility(ldud_id))


@bp.route('/api/module/LDUD01/close', methods=['POST'])
@login_required
def close():
    config = get_module_config('LDUD01')
    is_approver = str(config.get('approver_id', '')) == str(session.get('user_id')) or session.get('is_admin')
    if not is_approver:
        return jsonify({'error': 'No permission to close'}), 403
    data = request.json
    record_id = data.get('id')
    close_type = data.get('close_type')
    password = (data.get('password') or '').strip()
    if not record_id:
        return jsonify({'error': 'Missing id'}), 400
    if close_type not in ['Closed', 'Partial Close']:
        return jsonify({'error': 'Invalid close type'}), 400
    if not password:
        return jsonify({'error': 'Password is required'}), 400

    # Verify password server-side
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT id, password FROM users WHERE id=%s', [session.get('user_id')])
    user = cur.fetchone()
    conn.close()
    if not user or not check_password_hash(user['password'], password):
        return jsonify({'error': 'Incorrect password'}), 403

    # Re-verify eligibility server-side
    eligibility = model.get_closure_eligibility(record_id)
    if not eligibility['eligible']:
        return jsonify({'error': 'Record not eligible for closure', 'missing': eligibility['missing']}), 400
    if close_type == 'Closed' and not eligibility['can_full_close']:
        return jsonify({'error': f"Operations total ({eligibility['ops_total']}) does not match BL total ({eligibility['bl_total']}) — use Partial Close instead"}), 400

    # Enforce: at least one Proof of Quantity document must be uploaded
    conn_doc = get_db()
    cur_doc = get_cursor(conn_doc)
    cur_doc.execute('SELECT COUNT(*) FROM ldud_proof_documents WHERE ldud_id=%s', [record_id])
    doc_count = cur_doc.fetchone()['count']
    conn_doc.close()
    if doc_count == 0:
        return jsonify({'error': 'At least one Proof of Quantity document must be uploaded before closing'}), 400

    model.close_record(record_id, close_type, session.get('username'))
    # Queue notification to approver
    try:
        cfg = get_module_config('LDUD01')
        approver_email, approver_name = _get_user_email_by_id(cfg.get('approver_id'))
        if approver_email:
            # Fetch doc_num and vessel name for the notification
            _conn = get_db()
            _cur = get_cursor(_conn)
            _cur.execute(
                'SELECT lh.doc_num, vh.vessel_name FROM ldud_header lh LEFT JOIN vcn_header vh ON vh.id = lh.vcn_id WHERE lh.id=%s',
                [record_id]
            )
            _row = _cur.fetchone()
            _conn.close()
            doc_num = _row['doc_num'] if _row else f'#{record_id}'
            vessel_name = (_row['vessel_name'] or '—') if _row else '—'
            badge_color = '#059669' if close_type == 'Closed' else '#d97706'
            ldud_url = request.host_url.rstrip('/') + f'/module/LDUD01/'
            _queue_mail(
                to_email=approver_email,
                to_name=approver_name,
                subject=f"[Portbird DPPL] LDUD {doc_num} — {close_type}",
                body_html=_build_approval_mail_html(
                    approver_name=approver_name,
                    action_label=close_type,
                    subtitle='Lay / Despatch — Closure Notification',
                    details=[
                        ('Document No', doc_num),
                        ('Vessel',      vessel_name),
                        ('Status',      close_type),
                    ],
                    action_url=ldud_url,
                    action_btn_label='View in Portbird',
                    submitted_by=session.get('username'),
                    badge_color=badge_color,
                ),
                module_code='LDUD01',
                ref_id=record_id,
            )
            _trigger_mail_processing()
    except Exception:
        pass
    return jsonify({'doc_status': close_type})


@bp.route('/api/module/LDUD01/reopen', methods=['POST'])
@login_required
def reopen():
    config = get_module_config('LDUD01')
    is_approver = str(config.get('approver_id', '')) == str(session.get('user_id')) or session.get('is_admin')
    if not is_approver:
        return jsonify({'error': 'No permission to reopen'}), 403
    data = request.json
    record_id = data.get('id')
    comment = (data.get('comment') or '').strip()
    if not record_id:
        return jsonify({'error': 'Missing id'}), 400
    if not comment:
        return jsonify({'error': 'A reason is required when sending back to Draft'}), 400
    model.reopen_record(record_id, comment, session.get('username'))
    # Queue notification to the operator who last closed this record
    try:
        closer_email, closer_name = _get_closer_email(record_id)
        if closer_email:
            _queue_mail(
                to_email=closer_email,
                to_name=closer_name,
                subject=f"[PORTMAN] LDUD01 Record #{record_id} — Sent Back to Draft",
                body_html=f"""<p>Hello {closer_name or ''},</p>
<p>LDUD01 record <strong>#{record_id}</strong> has been <strong>sent back to Draft</strong>
by <strong>{session.get('username')}</strong>.</p>
<p><strong>Reason:</strong> {comment}</p>
<p>Please review and resubmit in PORTMAN.</p>
<hr><p style="color:#888;font-size:11px;">Automated notification from PORTMAN.</p>""",
                module_code='LDUD01',
                ref_id=record_id,
            )
            _trigger_mail_processing()
    except Exception:
        pass
    return jsonify({'doc_status': 'Draft'})


@bp.route('/api/module/LDUD01/closure-log/<int:record_id>')
@login_required
def closure_log(record_id):
    return jsonify(model.get_closure_log(record_id))

@bp.route('/api/module/LDUD01/delete', methods=['POST'])
@login_required
def delete():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_header(request.json['id'])
    return jsonify({'success': True})

# Delays sub-table endpoints
@bp.route('/api/module/LDUD01/delays/<int:ldud_id>')
@login_required
def get_delays(ldud_id):
    return jsonify(model.get_delays(ldud_id))

@bp.route('/api/module/LDUD01/delays/save', methods=['POST'])
@login_required
def save_delay():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    data = request.json
    row_id, total_mins, total_hrs = model.save_delay(data)
    return jsonify({'id': row_id, 'success': True, 'total_time_mins': total_mins, 'total_time_hrs': total_hrs})

@bp.route('/api/module/LDUD01/delays/delete', methods=['POST'])
@login_required
def delete_delay():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_delay(request.json['id'])
    return jsonify({'success': True})

# Barge Lines sub-table endpoints
@bp.route('/api/module/LDUD01/barge_lines/<int:ldud_id>')
@login_required
def get_barge_lines(ldud_id):
    return jsonify(model.get_barge_lines(ldud_id))

@bp.route('/api/module/LDUD01/lueu_barge_progress/<int:ldud_id>')
@login_required
def lueu_barge_progress(ldud_id):
    return jsonify(model.get_lueu_barge_progress(ldud_id))

@bp.route('/api/module/LDUD01/barge_lines/save', methods=['POST'])
@login_required
def save_barge_line():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    data = request.json
    try:
        row_id, trip_number = model.save_barge_line(data)
    except psycopg2.errors.UniqueViolation:
        return jsonify({
            'error': f"Trip {data.get('trip_number')} for barge "
                     f"'{data.get('barge_name')}' already has cargo "
                     f"'{data.get('cargo_name') or '(blank)'}' on this LDUD document."
        }), 400
    return jsonify({'id': row_id, 'success': True, 'trip_number': trip_number})

@bp.route('/api/module/LDUD01/barge_lines/delete', methods=['POST'])
@login_required
def delete_barge_line():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_barge_line(request.json['id'])
    return jsonify({'success': True})

# Anchorage Recording sub-table endpoints
@bp.route('/api/module/LDUD01/anchorage/<int:ldud_id>')
@login_required
def get_anchorage(ldud_id):
    return jsonify(model.get_anchorage(ldud_id))

@bp.route('/api/module/LDUD01/anchorage/save', methods=['POST'])
@login_required
def save_anchorage():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    row_id = model.save_anchorage(request.json)
    return jsonify({'id': row_id, 'success': True})

@bp.route('/api/module/LDUD01/anchorage/delete', methods=['POST'])
@login_required
def delete_anchorage():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_anchorage(request.json['id'])
    return jsonify({'success': True})

# Vessel Operations sub-table endpoints
@bp.route('/api/module/LDUD01/vessel_ops/<int:ldud_id>')
@login_required
def get_vessel_operations(ldud_id):
    return jsonify(model.get_vessel_operations(ldud_id))

@bp.route('/api/module/LDUD01/vessel_ops/save', methods=['POST'])
@login_required
def save_vessel_operation():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    row_id = model.save_vessel_operation(request.json)
    return jsonify({'id': row_id, 'success': True})

@bp.route('/api/module/LDUD01/vessel_ops/delete', methods=['POST'])
@login_required
def delete_vessel_operation():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_vessel_operation(request.json['id'])
    return jsonify({'success': True})

# Barge Cleaning Lines sub-table endpoints
@bp.route('/api/module/LDUD01/barge_cleaning/<int:ldud_id>')
@login_required
def get_barge_cleaning(ldud_id):
    return jsonify(model.get_barge_cleaning(ldud_id))

@bp.route('/api/module/LDUD01/barge_cleaning/save', methods=['POST'])
@login_required
def save_barge_cleaning():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    row_id = model.save_barge_cleaning(request.json)
    return jsonify({'id': row_id, 'success': True})

@bp.route('/api/module/LDUD01/barge_cleaning/delete', methods=['POST'])
@login_required
def delete_barge_cleaning():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_barge_cleaning(request.json['id'])
    return jsonify({'success': True})

# Hold Completion sub-table endpoints
@bp.route('/api/module/LDUD01/hold_completion/<int:ldud_id>')
@login_required
def get_hold_completion(ldud_id):
    return jsonify(model.get_hold_completion(ldud_id))

@bp.route('/api/module/LDUD01/hold_completion/save', methods=['POST'])
@login_required
def save_hold_completion():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    data = request.json
    if data.get('commenced') and data.get('completed'):
        from datetime import datetime
        try:
            start = datetime.fromisoformat(data['commenced'].replace(' ', 'T'))
            end = datetime.fromisoformat(data['completed'].replace(' ', 'T'))
            if end <= start:
                return jsonify({'error': 'Completed must be after Commenced'}), 400
        except ValueError:
            pass
    row_id = model.save_hold_completion(data)
    return jsonify({'id': row_id, 'success': True})

@bp.route('/api/module/LDUD01/hold_completion/delete', methods=['POST'])
@login_required
def delete_hold_completion():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_hold_completion(request.json['id'])
    return jsonify({'success': True})

# Hold Cargo Config endpoints
@bp.route('/api/module/LDUD01/hold_cargo/<int:ldud_id>')
@login_required
def get_hold_cargo(ldud_id):
    return jsonify(model.get_hold_cargo(ldud_id))

@bp.route('/api/module/LDUD01/hold_cargo/save', methods=['POST'])
@login_required
def save_hold_cargo():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    data = request.json
    model.save_hold_cargo(data['ldud_id'], data['hold_name'], data.get('cargo_name', ''))
    return jsonify({'success': True})


# ── Proof of Quantity Documents (stored in DB as BYTEA) ──────────────────────

ALLOWED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png', '.xlsx', '.xls', '.csv', '.doc', '.docx'}


@bp.route('/api/module/LDUD01/proof_docs/upload', methods=['POST'])
@login_required
def upload_proof_docs():
    ldud_id = request.form.get('ldud_id')
    if not ldud_id:
        return jsonify({'error': 'Missing ldud_id'}), 400

    files = request.files.getlist('files')
    if not files:
        return jsonify({'error': 'No files provided'}), 400

    conn = get_db()
    cur = get_cursor(conn)
    saved = []
    for f in files:
        original = f.filename or ''
        ext = os.path.splitext(original)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        file_bytes = f.read()
        if not file_bytes:
            continue
        mime_type = f.mimetype or mimetypes.guess_type(original)[0] or 'application/octet-stream'
        cur.execute('''
            INSERT INTO ldud_proof_documents (ldud_id, original_filename, file_bytes, mime_type, uploaded_by)
            VALUES (%s, %s, %s, %s, %s) RETURNING id, original_filename, uploaded_at
        ''', [ldud_id, original, file_bytes, mime_type, session.get('username')])
        row = cur.fetchone()
        saved.append({'id': row['id'], 'original_filename': row['original_filename'],
                      'uploaded_at': str(row['uploaded_at'])[:16]})
    conn.commit()
    conn.close()

    if not saved:
        return jsonify({'error': 'No valid files uploaded (allowed: pdf, jpg, png, xlsx, csv, doc)'}), 400
    return jsonify({'success': True, 'docs': saved})


@bp.route('/api/module/LDUD01/proof_docs/<int:ldud_id>')
@login_required
def list_proof_docs(ldud_id):
    # Metadata-only query — never select file_bytes here, keeps the list fast.
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT id, original_filename, uploaded_by, uploaded_at
        FROM ldud_proof_documents WHERE ldud_id=%s ORDER BY uploaded_at
    ''', [ldud_id])
    docs = [{'id': r['id'], 'original_filename': r['original_filename'],
              'uploaded_by': r['uploaded_by'], 'uploaded_at': str(r['uploaded_at'])[:16]}
            for r in cur.fetchall()]
    conn.close()
    return jsonify({'docs': docs})


@bp.route('/api/module/LDUD01/proof_docs/file/<int:doc_id>')
@login_required
def serve_proof_doc(doc_id):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT original_filename, file_bytes, mime_type FROM ldud_proof_documents WHERE id=%s', [doc_id])
    row = cur.fetchone()
    conn.close()
    if not row or row['file_bytes'] is None:
        return 'Not found', 404
    ext = os.path.splitext(row['original_filename'])[1].lower()
    inline_types = {'.pdf', '.jpg', '.jpeg', '.png'}
    as_attachment = ext not in inline_types
    return send_file(
        io.BytesIO(bytes(row['file_bytes'])),
        download_name=row['original_filename'],
        mimetype=row['mime_type'] or 'application/octet-stream',
        as_attachment=as_attachment,
    )


@bp.route('/api/module/LDUD01/proof_docs/by_vcn/<int:vcn_id>')
@login_required
def proof_docs_by_vcn(vcn_id):
    """Return proof docs for the LDUD linked to a VCN (used by FIN01 billing/approval pages)."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT id FROM ldud_header WHERE vcn_id=%s ORDER BY id DESC LIMIT 1', [vcn_id])
    ldud = cur.fetchone()
    if not ldud:
        conn.close()
        return jsonify({'docs': [], 'ldud_id': None})
    ldud_id = ldud['id']
    cur.execute('''
        SELECT id, original_filename, uploaded_by, uploaded_at
        FROM ldud_proof_documents WHERE ldud_id=%s ORDER BY uploaded_at
    ''', [ldud_id])
    docs = [{'id': r['id'], 'original_filename': r['original_filename'],
              'uploaded_by': r['uploaded_by'], 'uploaded_at': str(r['uploaded_at'])[:16]}
            for r in cur.fetchall()]
    conn.close()
    return jsonify({'docs': docs, 'ldud_id': ldud_id})


@bp.route('/api/module/LDUD01/proof_docs/by_bill/<int:bill_id>')
@login_required
def proof_docs_by_bill(bill_id):
    """Return all proof docs for VCN cargo lines on a bill (used by FIN01 approval)."""
    conn = get_db()
    cur = get_cursor(conn)
    # Get VCN-type cargo source IDs from this bill's lines
    cur.execute('''
        SELECT DISTINCT cargo_source_type, cargo_source_id
        FROM bill_lines
        WHERE bill_id=%s AND cargo_source_type IN ('VCN_IMPORT', 'VCN_EXPORT')
          AND cargo_source_id IS NOT NULL
    ''', [bill_id])
    sources = cur.fetchall()

    all_docs = []
    seen_ldud = set()
    for src in sources:
        table = 'vcn_cargo_declaration' if src['cargo_source_type'] == 'VCN_IMPORT' else 'vcn_export_cargo_declaration'
        cur.execute(f'SELECT vcn_id FROM {table} WHERE id=%s', [src['cargo_source_id']])
        decl = cur.fetchone()
        if not decl:
            continue
        cur.execute('SELECT id FROM ldud_header WHERE vcn_id=%s ORDER BY id DESC LIMIT 1', [decl['vcn_id']])
        ldud = cur.fetchone()
        if not ldud or ldud['id'] in seen_ldud:
            continue
        seen_ldud.add(ldud['id'])
        cur.execute('''
            SELECT id, original_filename, uploaded_by, uploaded_at
            FROM ldud_proof_documents WHERE ldud_id=%s ORDER BY uploaded_at
        ''', [ldud['id']])
        for r in cur.fetchall():
            all_docs.append({'id': r['id'], 'original_filename': r['original_filename'],
                             'uploaded_by': r['uploaded_by'], 'uploaded_at': str(r['uploaded_at'])[:16]})
    conn.close()
    return jsonify({'docs': all_docs})
