from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from functools import wraps
from database import get_db, get_cursor, get_module_config, save_module_config
from werkzeug.security import generate_password_hash
import json
import os
import re

_LOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'logs', 'app.log'
)
_LOG_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+\[(\w+)\]\s+(\S+)\s+(\S+)\s+[—\-]+\s*(.+)$'
)
# Werkzeug access log: "GET /path HTTP/1.1" 404
_STATUS_FROM_ACCESS = re.compile(r'HTTP/\d+\.\d+["\s]+(\d{3})')
# Status code embedded anywhere in traceback (4xx/5xx only)
_STATUS_FROM_TB = re.compile(r'\b([45]\d\d)\b')

bp = Blueprint('admin', __name__, url_prefix='/admin')

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Not logged in'}), 401
        return f(*args, **kwargs)
    return decorated

@bp.route('/', strict_slashes=False)
@admin_required
def admin_panel():
    return render_template('admin.html')

@bp.route('/api/users')
@admin_required
def get_users():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT id, username, email, is_admin FROM users ORDER BY username')
    users = cur.fetchall()
    conn.close()
    return jsonify([dict(u) for u in users])

@bp.route('/api/users/add', methods=['POST'])
@admin_required
def add_user():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    email = data.get('email', '').strip() or None
    is_admin = 1 if data.get('is_admin') else 0

    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400

    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('INSERT INTO users (username, password, email, is_admin) VALUES (%s, %s, %s, %s) RETURNING id',
                    [username, generate_password_hash(password), email, is_admin])
        user_id = cur.fetchone()['id']
        conn.commit()
        conn.close()
        return jsonify({'id': user_id, 'username': username, 'is_admin': is_admin})
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({'error': 'Username already exists'}), 400

@bp.route('/api/users/reset-password', methods=['POST'])
@admin_required
def reset_password():
    data = request.json
    user_id = data.get('id')
    new_password = data.get('password', '').strip()
    if not user_id or not new_password:
        return jsonify({'error': 'User ID and new password required'}), 400
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('UPDATE users SET password = %s WHERE id = %s', [generate_password_hash(new_password), user_id])
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@bp.route('/api/users/edit', methods=['POST'])
@admin_required
def edit_user():
    data = request.json
    user_id = data.get('id')
    username = data.get('username', '').strip()
    email = data.get('email', '').strip() or None
    is_admin = 1 if data.get('is_admin') else 0

    if not user_id or not username:
        return jsonify({'error': 'User ID and username required'}), 400

    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute('UPDATE users SET username=%s, email=%s, is_admin=%s WHERE id=%s',
                    [username, email, is_admin, user_id])
        conn.commit()
        # Sync session if admin edited themselves
        conn.close()
        return jsonify({'success': True})
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({'error': 'Username already exists'}), 400


@bp.route('/api/users/send-reset-email', methods=['POST'])
@admin_required
def send_reset_email():
    import random
    import string
    import secrets
    import datetime
    from mail_service import queue_mail, process_mail_queue
    import threading

    data = request.json
    user_id = data.get('id')
    if not user_id:
        return jsonify({'error': 'User ID required'}), 400

    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT id, username, email FROM users WHERE id=%s', [user_id])
    user = cur.fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    if not user['email']:
        conn.close()
        return jsonify({'error': 'User has no email address. Edit the user to add one first.'}), 400

    # Expire existing tokens
    cur.execute('UPDATE password_reset_tokens SET used=TRUE WHERE user_id=%s AND used=FALSE', [user_id])

    otp_code = ''.join(random.choices(string.digits, k=6))
    reset_token = secrets.token_urlsafe(32)
    expires_at = datetime.datetime.now() + datetime.timedelta(minutes=15)

    cur.execute('''
        INSERT INTO password_reset_tokens (user_id, email, otp_code, reset_token, expires_at)
        VALUES (%s, %s, %s, %s, %s)
    ''', [user_id, user['email'], otp_code, reset_token, expires_at])
    conn.commit()
    conn.close()

    import urllib.parse
    login_url = request.host_url.rstrip('/') + '/login?reset=1&email=' + urllib.parse.quote(user['email'])

    body_html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#f7fafc;border-radius:10px;">
      <div style="text-align:center;margin-bottom:24px;">
        <h2 style="color:#2d3748;font-size:20px;margin:0;">Portbird - DPPL</h2>
        <p style="color:#718096;font-size:13px;margin:4px 0 0;">Password Reset Request</p>
      </div>
      <div style="background:#fff;border-radius:8px;padding:24px;border:1px solid #e2e8f0;">
        <p style="color:#2d3748;font-size:14px;margin:0 0 16px;">Hi <strong>{user['username']}</strong>,</p>
        <p style="color:#4a5568;font-size:13px;margin:0 0 20px;">An administrator has requested a password reset for your account. Use the OTP below to set a new password.</p>
        <div style="text-align:center;background:#ebf8ff;border-radius:8px;padding:20px;margin:0 0 24px;">
          <p style="color:#2b6cb0;font-size:12px;font-weight:600;margin:0 0 8px;letter-spacing:1px;text-transform:uppercase;">Your One-Time Password</p>
          <div style="font-size:36px;font-weight:700;letter-spacing:10px;color:#1a365d;font-family:monospace;">{otp_code}</div>
          <p style="color:#718096;font-size:11px;margin:10px 0 0;">Valid for 15 minutes</p>
        </div>
        <div style="text-align:center;margin:0 0 20px;">
          <a href="{login_url}" style="display:inline-block;background:linear-gradient(135deg,#ec1c24,#2544a7);color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;letter-spacing:0.5px;">Enter OTP &amp; Reset Password</a>
        </div>
        <p style="color:#718096;font-size:11px;margin:0 0 6px;">Or copy this link into your browser:</p>
        <p style="color:#4a90d9;font-size:11px;word-break:break-all;margin:0 0 16px;">{login_url}</p>
        <p style="color:#a0aec0;font-size:11px;margin:0;">If you did not expect this email, please contact your administrator.</p>
      </div>
      <p style="text-align:center;color:#a0aec0;font-size:10px;margin:16px 0 0;">Portbird - DPPL &mdash; Port Management System</p>
    </div>
    """

    queue_mail(user['email'], user['username'],
               'Password Reset OTP - Portbird DPPL', body_html, 'ADMIN', user_id)

    # Fire off a send attempt in background
    threading.Thread(target=process_mail_queue, daemon=True).start()

    return jsonify({'success': True, 'message': f"Reset OTP sent to {user['email']}"})


@bp.route('/api/users/delete', methods=['POST'])
@admin_required
def delete_user():
    data = request.json
    user_id = data.get('id')
    if user_id == session.get('user_id'):
        return jsonify({'error': 'Cannot delete yourself'}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM module_permissions WHERE user_id = %s', [user_id])
    cur.execute('DELETE FROM users WHERE id = %s', [user_id])
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@bp.route('/api/modules')
@admin_required
def get_modules():
    from app import MODULES
    return jsonify([{'code': k, 'name': v['name']} for k, v in MODULES.items() if k != 'ADMIN'])

@bp.route('/api/permissions/<module_code>')
@admin_required
def get_permissions(module_code):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT id, username FROM users')
    users = cur.fetchall()
    cur.execute('''
        SELECT user_id, can_read, can_add, can_edit, can_delete
        FROM module_permissions WHERE module_code = %s
    ''', [module_code])
    permissions = cur.fetchall()
    conn.close()

    perm_map = {p['user_id']: dict(p) for p in permissions}
    result = []
    for u in users:
        p = perm_map.get(u['id'], {'can_read': 0, 'can_add': 0, 'can_edit': 0, 'can_delete': 0})
        result.append({
            'user_id': u['id'],
            'username': u['username'],
            'can_read': p['can_read'],
            'can_add': p['can_add'],
            'can_edit': p['can_edit'],
            'can_delete': p['can_delete']
        })
    return jsonify(result)

@bp.route('/api/permissions/<module_code>/save', methods=['POST'])
@admin_required
def save_permissions(module_code):
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    for p in data:
        cur.execute('''
            INSERT INTO module_permissions (user_id, module_code, can_read, can_add, can_edit, can_delete)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(user_id, module_code) DO UPDATE SET
                can_read = %s, can_add = %s, can_edit = %s, can_delete = %s
        ''', [p['user_id'], module_code, p['can_read'], p['can_add'], p['can_edit'], p['can_delete'],
              p['can_read'], p['can_add'], p['can_edit'], p['can_delete']])
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@bp.route('/api/permissions/user/<int:user_id>')
@admin_required
def get_user_permissions(user_id):
    from app import MODULES
    all_modules = [{'code': k, 'name': v['name']} for k, v in MODULES.items() if k != 'ADMIN']
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT module_code, can_read, can_add, can_edit, can_delete
        FROM module_permissions WHERE user_id = %s
    ''', [user_id])
    perm_map = {r['module_code']: dict(r) for r in cur.fetchall()}
    conn.close()
    result = []
    for m in all_modules:
        p = perm_map.get(m['code'], {'can_read': 0, 'can_add': 0, 'can_edit': 0, 'can_delete': 0})
        result.append({
            'module_code': m['code'],
            'module_name': m['name'],
            'can_read': p['can_read'],
            'can_add': p['can_add'],
            'can_edit': p['can_edit'],
            'can_delete': p['can_delete'],
        })
    return jsonify(result)


@bp.route('/api/permissions/user/<int:user_id>/save', methods=['POST'])
@admin_required
def save_user_permissions(user_id):
    data = request.json  # list of {module_code, can_read, can_add, can_edit, can_delete}
    conn = get_db()
    cur = conn.cursor()
    for p in data:
        cur.execute('''
            INSERT INTO module_permissions (user_id, module_code, can_read, can_add, can_edit, can_delete)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(user_id, module_code) DO UPDATE SET
                can_read = %s, can_add = %s, can_edit = %s, can_delete = %s
        ''', [user_id, p['module_code'],
              p['can_read'], p['can_add'], p['can_edit'], p['can_delete'],
              p['can_read'], p['can_add'], p['can_edit'], p['can_delete']])
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@bp.route('/api/config/all')
@admin_required
def get_all_configs():
    from app import MODULES
    from database import get_module_config
    all_modules = [{'code': k, 'name': v['name']} for k, v in MODULES.items() if k != 'ADMIN']
    result = []
    for m in all_modules:
        cfg = get_module_config(m['code'])
        result.append({
            'module_code': m['code'],
            'module_name': m['name'],
            'approval_add': cfg.get('approval_add', False),
            'approval_edit': cfg.get('approval_edit', False),
            'approver_id': cfg.get('approver_id', None),
        })
    return jsonify(result)


@bp.route('/api/config/<module_code>')
@login_required
def get_config(module_code):
    config = get_module_config(module_code)
    return jsonify(config)

@bp.route('/api/config/<module_code>/save', methods=['POST'])
@admin_required
def save_config(module_code):
    existing = get_module_config(module_code) or {}
    existing.update(request.json or {})
    save_module_config(module_code, existing)
    return jsonify({'success': True})


# ── LDUD Vessel Closure Admin ─────────────────────────────────────────────────

# ── SAP Config ────────────────────────────────────────────────────────────────

@bp.route('/api/sap-config')
@admin_required
def get_sap_config():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM sap_api_config ORDER BY is_active DESC, id LIMIT 1')
    row = cur.fetchone()
    conn.close()
    return jsonify(dict(row) if row else {})

@bp.route('/api/sap-config/save', methods=['POST'])
@admin_required
def save_sap_config():
    data = request.json
    conn = get_db()
    cur = get_cursor(conn)
    now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    username = session.get('username')

    # Check if config already exists
    cur.execute('SELECT id FROM sap_api_config ORDER BY id LIMIT 1')
    existing = cur.fetchone()

    if existing:
        cur.execute('''UPDATE sap_api_config SET
            environment=%s, base_url=%s, token_url=%s,
            client_id=%s, client_secret=%s,
            company_code=%s, default_payment_term=%s, payment_term=%s,
            plant_code=%s, business_place=%s, section_code=%s,
            credit_control_area=%s,
            profit_center=%s, igst_tax_code=%s, cgst_tax_code=%s, currency=%s,
            tds_gl=%s, tcs_gl=%s, round_off_gl=%s,
            is_active=%s, updated_by=%s, updated_date=%s
            WHERE id=%s''', [
            data.get('environment', 'production'),
            data.get('base_url', ''),
            data.get('token_url', ''),
            data.get('client_id', ''),
            data.get('client_secret', ''),
            data.get('company_code', ''),
            data.get('default_payment_term', ''),
            data.get('default_payment_term', ''),
            data.get('plant_code', ''),
            data.get('business_place', ''),
            data.get('section_code', ''),
            data.get('credit_control_area', ''),
            data.get('profit_center', ''),
            data.get('igst_tax_code', ''),
            data.get('cgst_tax_code', ''),
            data.get('currency', 'INR'),
            data.get('tds_gl', ''),
            data.get('tcs_gl', ''),
            data.get('round_off_gl', ''),
            data.get('is_active', 1),
            username, now, existing['id']
        ])
    else:
        cur.execute('''INSERT INTO sap_api_config
            (environment, base_url, token_url,
             client_id, client_secret,
             company_code, default_payment_term, payment_term,
             plant_code, business_place, section_code,
             credit_control_area,
             profit_center, igst_tax_code, cgst_tax_code, currency,
             tds_gl, tcs_gl, round_off_gl,
             is_active, created_by, created_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''', [
            data.get('environment', 'production'),
            data.get('base_url', ''),
            data.get('token_url', ''),
            data.get('client_id', ''),
            data.get('client_secret', ''),
            data.get('company_code', ''),
            data.get('default_payment_term', ''),
            data.get('default_payment_term', ''),
            data.get('plant_code', ''),
            data.get('business_place', ''),
            data.get('section_code', ''),
            data.get('credit_control_area', ''),
            data.get('profit_center', ''),
            data.get('igst_tax_code', ''),
            data.get('cgst_tax_code', ''),
            data.get('currency', 'INR'),
            data.get('tds_gl', ''),
            data.get('tcs_gl', ''),
            data.get('round_off_gl', ''),
            data.get('is_active', 1),
            username, now
        ])
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@bp.route('/api/sap-config/test-connection', methods=['POST'])
@admin_required
def test_sap_connection():
    """Test OAuth token acquisition against the configured token URL (query-param credentials per PORTBIRD spec)."""
    data = request.json or {}
    token_url     = data.get('token_url')
    client_id     = data.get('client_id')
    client_secret = data.get('client_secret')
    if not all([token_url, client_id, client_secret]):
        return jsonify({'success': False, 'message': 'Missing token URL, client ID or secret'})
    try:
        import requests as req
        resp = req.post(token_url, params={
            'client_id':     client_id,
            'client_secret': client_secret,
            'grant_type':    'client_credentials',
        }, timeout=15)
        if resp.status_code == 200 and resp.json().get('access_token'):
            return jsonify({'success': True,
                            'message': f'Connected! Token expires in {resp.json().get("expires_in", "?")}s'})
        return jsonify({'success': False, 'message': f'HTTP {resp.status_code}: {resp.text[:200]}'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# ── Port Bank Accounts ────────────────────────────────────────────────────────

PORT_BANKS_TABLE = 'port_bank_accounts'

def _ensure_port_banks_table(cur):
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS {PORT_BANKS_TABLE} (
            id SERIAL PRIMARY KEY,
            bank_name TEXT,
            account_number TEXT,
            ifsc_code TEXT,
            account_holder_name TEXT,
            branch_name TEXT,
            pan TEXT,
            cin TEXT,
            corporate_office_address TEXT
        )
    ''')
    for col in ('pan', 'cin', 'corporate_office_address'):
        cur.execute(f'ALTER TABLE {PORT_BANKS_TABLE} ADD COLUMN IF NOT EXISTS {col} TEXT')

@bp.route('/api/port-banks')
@admin_required
def get_port_banks():
    conn = get_db()
    cur = get_cursor(conn)
    _ensure_port_banks_table(cur)
    cur.execute(f'SELECT * FROM {PORT_BANKS_TABLE} ORDER BY id')
    rows = cur.fetchall()
    conn.commit()
    conn.close()
    return jsonify([dict(r) for r in rows])

@bp.route('/api/port-banks/save', methods=['POST'])
@admin_required
def save_port_bank():
    data = request.json
    conn = get_db()
    cur = get_cursor(conn)
    _ensure_port_banks_table(cur)
    row_id = data.get('id')
    fields = ['bank_name', 'account_number', 'ifsc_code', 'account_holder_name', 'branch_name',
              'pan', 'cin', 'corporate_office_address']
    vals = [data.get(f, '') for f in fields]
    if row_id:
        sets = ', '.join(f'{f}=%s' for f in fields)
        cur.execute(f'UPDATE {PORT_BANKS_TABLE} SET {sets} WHERE id=%s', vals + [row_id])
    else:
        cols = ', '.join(fields)
        phs = ', '.join('%s' for _ in fields)
        cur.execute(f'INSERT INTO {PORT_BANKS_TABLE} ({cols}) VALUES ({phs}) RETURNING id', vals)
        row_id = cur.fetchone()['id']
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': row_id})

@bp.route('/api/port-banks/delete', methods=['POST'])
@admin_required
def delete_port_bank():
    row_id = request.json.get('id')
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(f'DELETE FROM {PORT_BANKS_TABLE} WHERE id=%s', (row_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ── SMTP Config ───────────────────────────────────────────────────────────────

@bp.route('/api/smtp-config')
@admin_required
def get_smtp_config_route():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM smtp_config ORDER BY id LIMIT 1')
    row = cur.fetchone()
    conn.close()
    return jsonify(dict(row) if row else {})


@bp.route('/api/smtp-config/save', methods=['POST'])
@admin_required
def save_smtp_config_route():
    data = request.json
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT id FROM smtp_config ORDER BY id LIMIT 1')
    existing = cur.fetchone()
    now = __import__('datetime').datetime.now()
    fields = ['host', 'port', 'username', 'password', 'from_email', 'from_name',
              'use_tls', 'is_enabled', 'schedule_minutes']
    vals = [data.get(f) for f in fields] + [session.get('username'), now]
    if existing:
        sets = ', '.join(f'{f}=%s' for f in fields)
        cur.execute(f'UPDATE smtp_config SET {sets}, updated_by=%s, updated_at=%s WHERE id=%s',
                    vals + [existing['id']])
    else:
        cols = ', '.join(fields + ['updated_by', 'updated_at'])
        phs = ', '.join('%s' for _ in fields + ['updated_by', 'updated_at'])
        cur.execute(f'INSERT INTO smtp_config ({cols}) VALUES ({phs})', vals)
    conn.commit()
    conn.close()
    try:
        from app import _reschedule_mail_job
        _reschedule_mail_job()
    except Exception:
        pass
    return jsonify({'success': True})


@bp.route('/api/mail-queue')
@admin_required
def get_mail_queue():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        SELECT id, to_email, to_name, subject, status, retry_count, max_retries,
               to_char(created_at, 'DD-MM-YYYY HH24:MI') AS created_at,
               to_char(sent_at,    'DD-MM-YYYY HH24:MI') AS sent_at,
               error_message, module_code, ref_id
        FROM mail_queue ORDER BY id DESC LIMIT 200
    """)
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/mail-queue/retry', methods=['POST'])
@admin_required
def retry_failed_mail():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("""
        UPDATE mail_queue SET status='pending', retry_count=0, error_message=NULL
        WHERE status='failed'
    """)
    count = cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'reset': count})


@bp.route('/api/mail-queue/send-now', methods=['POST'])
@admin_required
def send_mail_now():
    from mail_service import process_mail_queue
    try:
        process_mail_queue()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── LDUD Vessel Closure Admin ─────────────────────────────────────────────────

@bp.route('/api/ldud/vessels')
@admin_required
def get_ldud_vessels():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT id, doc_num, vessel_name, vcn_doc_num, operation_type, doc_status, created_by
        FROM ldud_header
        WHERE doc_status IN ('Closed', 'Partial Close')
        ORDER BY id DESC
    ''')
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/ldud/open_vessel', methods=['POST'])
@admin_required
def open_vessel():
    data = request.json
    ldud_id = data.get('id')
    if not ldud_id:
        return jsonify({'error': 'Missing id'}), 400
    conn = get_db()
    cur = get_cursor(conn)

    # Same billing line as MBC: a vessel whose cargo is billed stays closed.
    from modules.FIN01 import model as fin_model
    cur.execute('SELECT vcn_id FROM ldud_header WHERE id=%s', [ldud_id])
    row = cur.fetchone()
    state = fin_model.source_lock_state(cur, 'VCN', row['vcn_id']) if row else ''
    if state:
        conn.close()
        return jsonify({'error': fin_model.lock_message(state, 'vessel')}), 400

    cur.execute("SELECT COUNT(*) AS cnt FROM ldud_proof_documents WHERE ldud_id=%s", (ldud_id,))
    doc_count = cur.fetchone()['cnt']
    cur.execute("DELETE FROM ldud_proof_documents WHERE ldud_id=%s", (ldud_id,))

    cur.execute("UPDATE ldud_header SET doc_status='Draft' WHERE id=%s", (ldud_id,))
    cur.execute("""INSERT INTO approval_log (module_code, record_id, action, comment, actioned_by)
                   VALUES ('LDUD01', %s, 'Reopened by Admin', 'Manually reopened via Admin panel; %s proof doc(s) removed', %s)""",
                (ldud_id, doc_count, session.get('username')))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'docs_removed': doc_count})


@bp.route('/api/mbc/approvals')
@admin_required
def get_mbc_approvals():
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''
        SELECT id, doc_num, mbc_name, operation_type, cargo_name, doc_status, created_by
        FROM mbc_header
        WHERE doc_status = 'Approved'
        ORDER BY id DESC
    ''')
    rows = cur.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/aud-config')
@admin_required
def get_aud_config():
    cfg = get_module_config('AUD01') or {}
    return jsonify({'nginx_log_path': cfg.get('nginx_log_path', '')})


@bp.route('/api/aud-config/save', methods=['POST'])
@admin_required
def save_aud_config():
    cfg = get_module_config('AUD01') or {}
    cfg['nginx_log_path'] = (request.json or {}).get('nginx_log_path', '').strip()
    save_module_config('AUD01', cfg)
    return jsonify({'success': True})


@bp.route('/api/logs')
@admin_required
def get_logs():
    try:
        if not os.path.exists(_LOG_FILE):
            return jsonify({'entries': [], 'stats': {'total': 0, 'ERROR': 0, 'WARNING': 0, 'INFO': 0, 'other': 0, 'by_date': [], 'file_size': 0}})
        file_size = os.path.getsize(_LOG_FILE)
        with open(_LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        entries = _parse_log_entries(content)
        stats = _calc_log_stats(entries)
        stats['file_size'] = file_size
        return jsonify({'entries': list(reversed(entries[-1000:])), 'stats': stats})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/logs/clear', methods=['POST'])
@admin_required
def clear_logs():
    try:
        with open(_LOG_FILE, 'w', encoding='utf-8') as f:
            f.write('')
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _extract_http_status(entry):
    m = _STATUS_FROM_ACCESS.search(entry['message'])
    if m:
        return int(m.group(1))
    tb = entry.get('traceback', '')
    if tb:
        m = _STATUS_FROM_TB.search(tb)
        if m:
            return int(m.group(1))
    return None


def _parse_log_entries(content):
    entries = []
    current = None
    for line in content.splitlines():
        m = _LOG_RE.match(line)
        if m:
            if current:
                current['http_status'] = _extract_http_status(current)
                entries.append(current)
            current = {
                'timestamp': m.group(1),
                'level': m.group(2),
                'logger': m.group(3),
                'source': m.group(4),
                'message': m.group(5).strip(),
                'traceback': '',
            }
        elif current is not None:
            current['traceback'] += line + '\n'
    if current:
        current['http_status'] = _extract_http_status(current)
        entries.append(current)
    return entries


def _calc_log_stats(entries):
    status_counts = {}
    no_status = 0
    by_date = {}
    for e in entries:
        s = e.get('http_status')
        day = e['timestamp'][:10]
        if day not in by_date:
            by_date[day] = {}
        if s:
            key = str(s)
            status_counts[key] = status_counts.get(key, 0) + 1
            by_date[day][key] = by_date[day].get(key, 0) + 1
        else:
            no_status += 1
            by_date[day]['none'] = by_date[day].get('none', 0) + 1
    return {
        'total': len(entries),
        'status_counts': status_counts,
        'no_status': no_status,
        'by_date': [{'date': d, **counts} for d, counts in sorted(by_date.items(), reverse=True)][:14],
    }


@bp.route('/api/mbc/reset_approval', methods=['POST'])
@admin_required
def reset_mbc_approval():
    data = request.json
    mbc_id = data.get('id')
    if not mbc_id:
        return jsonify({'error': 'Missing id'}), 400
    conn = get_db()
    cur = get_cursor(conn)

    # Admin override stops at the billing line: reopening a billed MBC would let
    # its customer or quantity be retyped under an already-issued bill.
    from modules.FIN01 import model as fin_model
    state = fin_model.source_lock_state(cur, 'MBC', mbc_id)
    if state:
        conn.close()
        return jsonify({'error': fin_model.lock_message(state, 'MBC')}), 400

    cur.execute("SELECT COUNT(*) AS cnt FROM mbc_proof_documents WHERE mbc_id=%s", (mbc_id,))
    doc_count = cur.fetchone()['cnt']
    cur.execute("DELETE FROM mbc_proof_documents WHERE mbc_id=%s", (mbc_id,))

    cur.execute("UPDATE mbc_header SET doc_status='Draft' WHERE id=%s", (mbc_id,))
    cur.execute("""INSERT INTO approval_log (module_code, record_id, action, comment, actioned_by)
                   VALUES ('MBC01', %s, 'Reopened by Admin', 'Approval reset via Admin panel; %s proof doc(s) removed', %s)""",
                (mbc_id, doc_count, session.get('username')))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'docs_removed': doc_count})


# ── SAP Inbound Tokens ────────────────────────────────────────────────────────

import sap_inbound  # noqa: E402

@bp.route('/api/sap-tokens')
@admin_required
def list_sap_tokens():
    return jsonify(sap_inbound.list_tokens())


@bp.route('/api/sap-tokens/generate', methods=['POST'])
@admin_required
def generate_sap_token():
    data = request.json or {}
    label = (data.get('label') or '').strip()
    if not label:
        return jsonify({'error': 'Label required'}), 400
    try:
        result = sap_inbound.generate_token(label, created_by=session.get('username'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'success': True, **result})


@bp.route('/api/sap-tokens/revoke', methods=['POST'])
@admin_required
def revoke_sap_token():
    token_id = (request.json or {}).get('id')
    if not token_id:
        return jsonify({'error': 'id required'}), 400
    sap_inbound.revoke_token(token_id, revoked_by=session.get('username'))
    return jsonify({'success': True})


@bp.route('/api/sap-tokens/reactivate', methods=['POST'])
@admin_required
def reactivate_sap_token():
    token_id = (request.json or {}).get('id')
    if not token_id:
        return jsonify({'error': 'id required'}), 400
    sap_inbound.reactivate_token(token_id)
    return jsonify({'success': True})


# ── SAP Playground ────────────────────────────────────────────────────────────

@bp.route('/api/sap-playground/docs')
@admin_required
def playground_docs():
    """List recent invoices + FDCN docs for the playground dropdown."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''SELECT id, invoice_number AS doc_number, 'Invoice' AS doc_kind,
                          customer_name, total_amount, invoice_status AS status,
                          invoice_date AS doc_date
                   FROM invoice_header ORDER BY id DESC LIMIT 100''')
    invs = [dict(r) for r in cur.fetchall()]
    cur.execute('''SELECT id, doc_number, doc_type AS doc_kind,
                          customer_name, total_amount, doc_status AS status,
                          doc_date FROM fdcn_header ORDER BY id DESC LIMIT 100''')
    fdcns = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({'invoices': invs, 'fdcns': fdcns})


@bp.route('/api/sap-playground/build-payload', methods=['POST'])
@admin_required
def playground_build_payload():
    """Build outbound payload for a chosen doc (no SAP send)."""
    data = request.json or {}
    kind = (data.get('kind') or '').lower()
    doc_id = data.get('id')
    variant = (data.get('variant') or 'post').lower()  # 'post' or 'reversal'
    if not doc_id:
        return jsonify({'error': 'id required'}), 400

    import sap_builder
    try:
        if kind == 'invoice':
            from modules.FIN01 import model as fin_model
            inv = fin_model.get_invoice_by_id(doc_id)
            if not inv:
                return jsonify({'error': 'Invoice not found'}), 404
            lines = fin_model.get_invoice_lines(doc_id)
            if variant == 'reversal':
                payload = sap_builder.build_invoice_reversal_payload(inv, lines)
            else:
                payload = sap_builder.build_invoice_payload(inv, lines)
        elif kind in ('fdcn', 'dn', 'cn'):
            from modules.FDCN01 import model as fdcn_model
            header = fdcn_model.get_fdcn_by_id(doc_id)
            if not header:
                return jsonify({'error': 'FDCN doc not found'}), 404
            lines = fdcn_model.get_fdcn_lines(doc_id)
            payload = sap_builder.build_fdcn_payload(header, lines)
        else:
            return jsonify({'error': 'Unknown kind'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'payload': payload})


@bp.route('/api/sap-playground/send', methods=['POST'])
@admin_required
def playground_send():
    """POST a (possibly user-edited) payload to SAP and return raw result."""
    data = request.json or {}
    payload = data.get('payload')
    reference = (data.get('reference') or 'PLAYGROUND').strip()
    if not isinstance(payload, dict):
        return jsonify({'error': 'payload (object) required'}), 400

    import sap_client
    result = sap_client.post_invoice_to_sap(
        payload, 'Playground', 0, reference, session.get('username')
    )
    return jsonify(result)


@bp.route('/api/sap-playground/inbound-logs')
@admin_required
def playground_inbound_logs():
    """List integration_logs rows where integration_type='SAP_INBOUND'."""
    page = int(request.args.get('page', 1))
    size = int(request.args.get('size', 50))
    ref = (request.args.get('ref') or '').strip()
    status_filter = (request.args.get('status') or '').strip()

    where = ["integration_type = 'SAP_INBOUND'"]
    params = []
    if ref:
        where.append("(request_body LIKE %s OR source_reference LIKE %s)")
        params.extend([f'%{ref}%', f'%{ref}%'])
    if status_filter:
        where.append('status = %s')
        params.append(status_filter)
    where_sql = 'WHERE ' + ' AND '.join(where)

    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(f'SELECT COUNT(*) AS cnt FROM integration_logs {where_sql}', params)
    total = cur.fetchone()['cnt']
    cur.execute(f'''SELECT id, source_reference AS token_label, request_body, response_body,
                           response_status_code, status, error_message, created_by, created_date
                    FROM integration_logs {where_sql}
                    ORDER BY id DESC LIMIT %s OFFSET %s''',
                params + [size, (page - 1) * size])
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({'data': rows, 'total': total, 'last_page': (total + size - 1) // size})


# ===== Cutover / Migration (go-live) =====

from modules.ADMIN import cutover as cutover_mod


@bp.route('/api/cutover/state')
@admin_required
def cutover_state():
    return jsonify({'locked': cutover_mod.is_locked(), 'seeds': cutover_mod.get_seeds()})


@bp.route('/api/cutover/invoice-seed', methods=['POST'])
@admin_required
def cutover_invoice_seed():
    d = request.json or {}
    try:
        start_seq = int(d.get('start_seq'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'start_seq must be an integer'}), 400
    ok, msg = cutover_mod.set_invoice_seed(
        (d.get('doc_series') or '').strip().rstrip('/').upper(),
        (d.get('financial_year') or '').strip(),
        start_seq,
        session.get('username'))
    if ok:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': msg}), 403 if 'locked' in msg.lower() else 400


@bp.route('/api/cutover/bill-seed', methods=['POST'])
@admin_required
def cutover_bill_seed():
    d = request.json or {}
    try:
        start_seq = int(d.get('start_seq'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'start_seq must be an integer'}), 400
    ok, msg = cutover_mod.set_bill_seed(start_seq, session.get('username'))
    if ok:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': msg}), 403 if 'locked' in msg.lower() else 400


@bp.route('/api/cutover/mark-billed', methods=['POST'])
@admin_required
def cutover_mark_billed():
    d = request.json or {}
    billed = bool(d.get('billed', True))
    ok, msg, counts = cutover_mod.mark_items_billed(
        d.get('cargo_items') or [], d.get('service_ids') or [],
        session.get('username'), billed=billed)
    if ok:
        return jsonify({'success': True, 'counts': counts})
    return jsonify({'success': False, 'error': msg}), 403 if 'locked' in msg.lower() else 400


@bp.route('/api/cutover/lock', methods=['POST'])
@admin_required
def cutover_lock():
    d = request.json or {}
    cutover_mod.set_lock(bool(d.get('locked', True)), session.get('username'))
    return jsonify({'success': True, 'locked': cutover_mod.is_locked()})


# ===== Uninvoice (remove invoices with no SAP document, reset bills) =====

@bp.route('/api/uninvoice/list')
@admin_required
def uninvoice_list():
    from modules.FIN01 import model as fin_model
    return jsonify(fin_model.get_uninvoiceable_invoices())


@bp.route('/api/uninvoice/do', methods=['POST'])
@admin_required
def uninvoice_do():
    from modules.FIN01 import model as fin_model
    invoice_id = (request.json or {}).get('id')
    if not invoice_id:
        return jsonify({'error': 'Missing id'}), 400
    result = fin_model.uninvoice_invoice(invoice_id, session.get('username'))
    if not result.get('ok'):
        return jsonify({'error': result.get('error', 'Uninvoice failed')}), 400
    return jsonify({'success': True, **result})


# ===== Release Cargo (free cargo billed to the wrong party) =====

@bp.route('/api/cargo-release/list')
@admin_required
def cargo_release_list():
    from modules.FIN01 import model as fin_model
    return jsonify(fin_model.get_billed_cargo(request.args.get('q', '')))


@bp.route('/api/cargo-release/do', methods=['POST'])
@admin_required
def cargo_release_do():
    from modules.FIN01 import model as fin_model
    d = request.json or {}
    if not d.get('cargo_source_type') or not d.get('cargo_source_id'):
        return jsonify({'error': 'Missing cargo reference'}), 400
    result = fin_model.release_cargo(
        d['cargo_source_type'], d['cargo_source_id'],
        session.get('username'), d.get('reason', ''))
    if not result.get('ok'):
        return jsonify({'error': result.get('error', 'Release failed')}), 400
    return jsonify({'success': True, **result})


# ===== Swap Bill Cargo (replace wrong cargo on a bill, amounts unchanged) =====

@bp.route('/api/cargo-swap/bills')
@admin_required
def cargo_swap_bills():
    from modules.FIN01 import model as fin_model
    return jsonify(fin_model.get_swappable_bills(
        request.args.get('q', ''),
        mismatched_only=request.args.get('mismatched') == '1'))


@bp.route('/api/cargo-swap/lines/<int:bill_id>')
@admin_required
def cargo_swap_lines(bill_id):
    from modules.FIN01 import model as fin_model
    return jsonify({
        'lines': fin_model.get_bill_cargo_lines(bill_id),
        'candidates': fin_model.get_swap_candidates(bill_id),
    })


@bp.route('/api/cargo-swap/do', methods=['POST'])
@admin_required
def cargo_swap_do():
    from modules.FIN01 import model as fin_model
    d = request.json or {}
    if not d.get('bill_id') or not d.get('bill_line_id'):
        return jsonify({'error': 'Missing bill or line reference'}), 400
    result = fin_model.swap_bill_cargo(
        d['bill_id'], d['bill_line_id'], d.get('replacements') or [],
        session.get('username'), d.get('reason', ''))
    if not result.get('ok'):
        return jsonify({'error': result.get('error', 'Swap failed')}), 400
    return jsonify({'success': True, **result})


# ===== Remove Bill (delete duplicate/unwanted bills, reset series) =====

@bp.route('/api/bill-remove/list')
@admin_required
def bill_remove_list():
    from modules.FIN01 import model as fin_model
    return jsonify(fin_model.get_removable_bills())


@bp.route('/api/bill-remove/do', methods=['POST'])
@admin_required
def bill_remove_do():
    from modules.FIN01 import model as fin_model
    bill_id = (request.json or {}).get('id')
    if not bill_id:
        return jsonify({'error': 'Missing id'}), 400
    result = fin_model.delete_bill(bill_id, session.get('username'))
    if not result.get('ok'):
        return jsonify({'error': result.get('error', 'Bill removal failed')}), 400
    return jsonify({'success': True, **result})
