from flask import render_template, request, redirect, url_for, session, jsonify
from . import bp
from . import model
from database import get_db, get_cursor, get_user_permissions, get_module_config
from modules.VAM01 import model as vam01_model
from modules.VCUM01 import model as vcum_model
from modules.FSTM01 import model as fstm_model
from modules.FCRM01 import model as fcrm_model
from modules.VCG01 import model as vcg_model
from mail_service import notify_module_approver, get_module_approver_info, build_approval_mail_html


def _queue_agreement_approval_request(agreement_id, agreement_code, customer_name, agreement_name):
    info = get_module_approver_info('FCAM01')
    if not info.get('approval_add'):
        return
    agreement_url = request.host_url.rstrip('/') + url_for('FCAM01.entry', agreement_id=agreement_id)
    notify_module_approver(
        module_code='FCAM01',
        ref_id=agreement_id,
        subject=f"[Portbird DPPL] Agreement {agreement_code} — Pending Approval",
        body_html=build_approval_mail_html(
            approver_name=info.get('username'),
            action_label='Pending Approval',
            subtitle='Customer Agreement — Approval Required',
            details=[
                ('Agreement Code', agreement_code or '—'),
                ('Customer',       customer_name or '—'),
                ('Agreement Name', agreement_name or '—'),
            ],
            action_url=agreement_url,
            action_btn_label='Review &amp; Approve Agreement',
            submitted_by=session.get('username'),
            badge_color='#d97706',
        ),
    )

@bp.route('/module/FCAM01/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    perms = get_user_permissions(session['user_id'], 'FCAM01')
    page = int(request.args.get('page', 1))
    data, total = model.get_agreement_data(page)

    return render_template('fcam01.html',
                         data=data,
                         page=page,
                         last_page=(total + 19) // 20,
                         perms=perms,
                         username=session.get('username'))


@bp.route('/module/FCAM01/entry')
@bp.route('/module/FCAM01/entry/<int:agreement_id>')
def entry(agreement_id=None):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    perms = get_user_permissions(session['user_id'], 'FCAM01')

    # Get master data
    # ponytail: get_data is paginated (default 20 rows) — fetch all for dropdowns
    agents = vam01_model.get_data(1, 100000)[0] or []
    customers = vcum_model.get_data(1, 100000)[0] or []
    service_types = fstm_model.get_all_service_types() or []
    currencies = fcrm_model.get_all_currencies() or []
    cargo_list = vcg_model.get_all() or []

    # Identify cargo handling service type IDs (CHGL01, CHGU01)
    cargo_service_ids = [s['id'] for s in service_types
                         if s.get('service_code') in ('CHGL01', 'CHGU01')]

    header_data = None
    lines_data = []

    if agreement_id:
        conn = get_db()
        cur = get_cursor(conn)
        cur.execute('SELECT * FROM customer_agreements WHERE id=%s', (agreement_id,))
        header_data = dict(cur.fetchone())
        conn.close()
        lines_data = model.get_agreement_lines(agreement_id)

    return render_template('entry.html',
                         header=header_data,
                         lines=lines_data,
                         agents=agents,
                         customers=customers,
                         service_types=service_types,
                         currencies=currencies,
                         cargo_list=cargo_list,
                         cargo_service_ids=cargo_service_ids,
                         perms=perms,
                         username=session.get('username'))


@bp.route('/api/module/FCAM01/save-header', methods=['POST'])
def save_header():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    perms = get_user_permissions(session['user_id'], 'FCAM01')
    data = request.json

    if data.get('id') and not perms['can_edit']:
        return jsonify({'success': False, 'error': 'No edit permission'})
    if not data.get('id') and not perms['can_add']:
        return jsonify({'success': False, 'error': 'No add permission'})

    data['created_by'] = session.get('username')
    data['created_date'] = __import__('datetime').datetime.now().strftime('%Y-%m-%d')

    # Set status based on approval config
    config = get_module_config('FCAM01')
    user_id = session.get('user_id')
    is_approver = str(config.get('approver_id', '')) == str(user_id)
    is_admin = session.get('is_admin')

    if not data.get('id'):  # New agreement
        if config.get('approval_add'):
            data['agreement_status'] = 'Pending'
        else:
            data['agreement_status'] = 'Draft'

    row_id, agreement_code = model.save_agreement_header(data)

    if data.get('agreement_status') == 'Pending':
        _queue_agreement_approval_request(
            row_id,
            agreement_code,
            data.get('customer_name'),
            data.get('agreement_name'),
        )

    return jsonify({'success': True, 'id': row_id, 'agreement_code': agreement_code})


@bp.route('/api/module/FCAM01/save-line', methods=['POST'])
def save_line():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    perms = get_user_permissions(session['user_id'], 'FCAM01')
    if not perms['can_add'] and not perms['can_edit']:
        return jsonify({'success': False, 'error': 'No permission'})

    data = request.json
    row_id = model.save_agreement_line(data)
    return jsonify({'success': True, 'id': row_id})


@bp.route('/api/module/FCAM01/save-lines-batch', methods=['POST'])
def save_lines_batch():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    perms = get_user_permissions(session['user_id'], 'FCAM01')
    if not perms['can_add'] and not perms['can_edit']:
        return jsonify({'success': False, 'error': 'No permission'})

    lines = request.json.get('lines', [])
    results = []
    for line in lines:
        try:
            row_id = model.save_agreement_line(line)
            results.append({'success': True, 'id': row_id, 'cargo_id': line.get('cargo_id')})
        except Exception as e:
            results.append({'success': False, 'error': str(e), 'cargo_id': line.get('cargo_id')})
    return jsonify({'success': True, 'results': results})


@bp.route('/api/module/FCAM01/delete-line', methods=['POST'])
def delete_line():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    perms = get_user_permissions(session['user_id'], 'FCAM01')
    if not perms['can_delete']:
        return jsonify({'success': False, 'error': 'No delete permission'})

    row_id = request.json.get('id')
    model.delete_agreement_line(row_id)
    return jsonify({'success': True})


@bp.route('/api/module/FCAM01/delete-header', methods=['POST'])
def delete_header():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    perms = get_user_permissions(session['user_id'], 'FCAM01')
    if not perms['can_delete']:
        return jsonify({'success': False, 'error': 'No delete permission'})

    row_id = request.json.get('id')

    # Check if agreement is used in any bill
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT COUNT(*) as cnt FROM bill_header WHERE agreement_id = %s', [row_id])
    bill_count = cur.fetchone()['cnt']
    conn.close()
    if bill_count > 0:
        return jsonify({'success': False, 'error': f'Cannot delete — this agreement is used in {bill_count} bill(s)'})

    model.delete_agreement_header(row_id)
    return jsonify({'success': True})


@bp.route('/api/module/FCAM01/approve', methods=['POST'])
def approve():
    """Approve agreement - only approver or admin"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})

    config = get_module_config('FCAM01')
    user_id = session.get('user_id')
    is_approver = str(config.get('approver_id', '')) == str(user_id)
    is_admin = session.get('is_admin')

    if not is_approver and not is_admin:
        return jsonify({'success': False, 'error': 'Only approver or admin can approve agreements'})

    agreement_id = request.json.get('id')
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('''UPDATE customer_agreements
        SET agreement_status='Approved', approved_by=%s, approved_date=%s
        WHERE id=%s''',
        [session.get('username'), __import__('datetime').datetime.now().strftime('%Y-%m-%d'), agreement_id])
    conn.commit()
    conn.close()

    return jsonify({'success': True})
