import json as _json
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from functools import wraps
from . import model
from database import get_user_permissions, get_module_config

bp = Blueprint('VCN01', __name__, template_folder='.')
MODULE_CODE = 'VCN01'

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


def _is_approver():
    config = get_module_config(MODULE_CODE)
    return (str(config.get('approver_id', '')) == str(session.get('user_id'))
            or bool(session.get('is_admin')))


def _vcn_id_of(table, row_id):
    """Owning vcn_id for a sub-table row, so a line edit is gated by its parent."""
    if not row_id:
        return None
    from database import get_db, get_cursor
    conn = get_db()
    cur = get_cursor(conn)
    # table is one of the fixed names passed by the routes below, never input.
    cur.execute(f'SELECT vcn_id FROM {table} WHERE id=%s', [row_id])
    row = cur.fetchone()
    conn.close()
    return row['vcn_id'] if row else None


def _check_editable(vcn_id):
    """Gate every VCN write. Returns an error response, or None when allowed.

    Mirrors MBC01: cargo that has been billed or invoiced freezes the vessel
    call, because a retro-edited cargo declaration silently rewrites what a
    customer was charged. Approved but unbilled stays admin-resettable."""
    if not vcn_id:
        return None
    from modules.FIN01 import model as fin_model
    from database import get_db, get_cursor
    conn = get_db()
    cur = get_cursor(conn)
    try:
        state = fin_model.source_lock_state(cur, 'VCN', vcn_id)
    finally:
        conn.close()
    if state:
        return jsonify({'error': fin_model.lock_message(state, 'vessel call')}), 403
    if model.get_doc_status(vcn_id) == 'Approved' and not _is_approver():
        return jsonify({'error': 'This VCN is Approved and locked for editing. '
                                 'Ask an admin to reopen it from the Admin panel.'}), 403
    return None

@bp.route('/module/VCN01/')
@login_required
def view():
    perms = get_perms()
    if not perms.get('can_read'):
        return render_template('no_access.html'), 403
    return render_template('vcn01.html', permissions=perms)

@bp.route('/api/module/VCN01/data')
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
    data, total = model.get_data(page, size, filters)
    return jsonify({'data': data, 'last_page': (total + size - 1) // size, 'total': total})

@bp.route('/api/module/VCN01/vessels')
@login_required
def get_vessels():
    return jsonify(model.get_vessels())

@bp.route('/api/module/VCN01/save', methods=['POST'])
@login_required
def save():
    perms = get_perms()
    data = request.json
    is_new = not data.get('id')

    if is_new and not perms.get('can_add'):
        return jsonify({'error': 'No permission to add'}), 403
    if not is_new and not perms.get('can_edit'):
        return jsonify({'error': 'No permission to edit'}), 403

    if not is_new:
        blocked = _check_editable(data['id'])
        if blocked:
            return blocked
        data['doc_status'] = ('Approved' if model.get_doc_status(data['id']) == 'Approved'
                              else 'Draft')
    else:
        data['doc_status'] = 'Draft'

    row_id, doc_num = model.save_header(data)
    return jsonify({'success': True, 'id': row_id, 'vcn_doc_num': doc_num, 'doc_status': data.get('doc_status')})


@bp.route('/api/module/VCN01/approval_check/<int:record_id>')
@login_required
def approval_check(record_id):
    return jsonify(model.get_approval_eligibility(record_id))


@bp.route('/api/module/VCN01/approve', methods=['POST'])
@login_required
def approve():
    config = get_module_config('VCN01')
    is_approver = str(config.get('approver_id', '')) == str(session.get('user_id')) or session.get('is_admin')
    if not is_approver:
        return jsonify({'error': 'No permission to approve'}), 403
    record_id = request.json.get('id')
    if not record_id:
        return jsonify({'error': 'Missing id'}), 400
    eligibility = model.get_approval_eligibility(record_id)
    if not eligibility['eligible']:
        return jsonify({'error': 'Record not eligible for approval', 'missing': eligibility['missing']}), 400
    model.approve_record(record_id, session.get('username'))
    return jsonify({'doc_status': 'Approved'})


@bp.route('/api/module/VCN01/send_back', methods=['POST'])
@login_required
def send_back():
    config = get_module_config('VCN01')
    is_approver = str(config.get('approver_id', '')) == str(session.get('user_id')) or session.get('is_admin')
    if not is_approver:
        return jsonify({'error': 'No permission'}), 403
    data = request.json
    record_id = data.get('id')
    comment = (data.get('comment') or '').strip()
    if not record_id:
        return jsonify({'error': 'Missing id'}), 400
    if not comment:
        return jsonify({'error': 'A reason is required when sending back to Draft'}), 400
    blocked = _check_editable(record_id)
    if blocked:
        return blocked
    model.send_back_to_draft(record_id, comment, session.get('username'))
    return jsonify({'doc_status': 'Draft'})


@bp.route('/api/module/VCN01/approval-log/<int:record_id>')
@login_required
def approval_log(record_id):
    return jsonify(model.get_approval_log(record_id))


@bp.route('/api/module/VCN01/delete', methods=['POST'])
@login_required
def delete():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    blocked = _check_editable(request.json.get('id'))
    if blocked:
        return blocked
    model.delete_header(request.json.get('id'))
    return jsonify({'success': True})

# Nomination endpoints
@bp.route('/api/module/VCN01/nominations/<int:vcn_id>')
@login_required
def get_nominations(vcn_id):
    return jsonify(model.get_nominations(vcn_id))

@bp.route('/api/module/VCN01/nominations/save', methods=['POST'])
@login_required
def save_nomination():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    row_id = model.save_nomination(request.json)
    return jsonify({'success': True, 'id': row_id})

@bp.route('/api/module/VCN01/nominations/delete', methods=['POST'])
@login_required
def delete_nomination():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_nomination(request.json.get('id'))
    return jsonify({'success': True})

# Delays endpoints
@bp.route('/api/module/VCN01/delays/<int:vcn_id>')
@login_required
def get_delays(vcn_id):
    return jsonify(model.get_delays(vcn_id))

@bp.route('/api/module/VCN01/delays/save', methods=['POST'])
@login_required
def save_delay():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    row_id = model.save_delay(request.json)
    return jsonify({'success': True, 'id': row_id})

@bp.route('/api/module/VCN01/delays/delete', methods=['POST'])
@login_required
def delete_delay():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_delay(request.json.get('id'))
    return jsonify({'success': True})

# Cargo Declaration endpoints
@bp.route('/api/module/VCN01/cargo/<int:vcn_id>')
@login_required
def get_cargo(vcn_id):
    return jsonify(model.get_cargo_declarations(vcn_id))

@bp.route('/api/module/VCN01/cargo/save', methods=['POST'])
@login_required
def save_cargo():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    data = request.json
    blocked = _check_editable(data.get('vcn_id') or _vcn_id_of('vcn_cargo_declaration', data.get('id')))
    if blocked:
        return blocked
    row_id = model.save_cargo_declaration(data)
    return jsonify({'success': True, 'id': row_id})

@bp.route('/api/module/VCN01/cargo/delete', methods=['POST'])
@login_required
def delete_cargo():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    blocked = _check_editable(_vcn_id_of('vcn_cargo_declaration', request.json.get('id')))
    if blocked:
        return blocked
    model.delete_cargo_declaration(request.json.get('id'))
    return jsonify({'success': True})

# Export Cargo Declaration endpoints
@bp.route('/api/module/VCN01/export_cargo/<int:vcn_id>')
@login_required
def get_export_cargo(vcn_id):
    return jsonify(model.get_export_cargo_declarations(vcn_id))

@bp.route('/api/module/VCN01/export_cargo/save', methods=['POST'])
@login_required
def save_export_cargo():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    data = request.json
    blocked = _check_editable(data.get('vcn_id') or _vcn_id_of('vcn_export_cargo_declaration', data.get('id')))
    if blocked:
        return blocked
    row_id = model.save_export_cargo_declaration(data)
    return jsonify({'success': True, 'id': row_id})

@bp.route('/api/module/VCN01/export_cargo/delete', methods=['POST'])
@login_required
def delete_export_cargo():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    blocked = _check_editable(_vcn_id_of('vcn_export_cargo_declaration', request.json.get('id')))
    if blocked:
        return blocked
    model.delete_export_cargo_declaration(request.json.get('id'))
    return jsonify({'success': True})

@bp.route('/api/module/VCN01/export_cargo_names/<int:vcn_id>')
@login_required
def get_export_cargo_names(vcn_id):
    return jsonify(model.get_export_cargo_names_for_vcn(vcn_id))

@bp.route('/api/module/VCN01/all_cargo_names/<int:vcn_id>')
@login_required
def get_all_cargo_names(vcn_id):
    return jsonify(model.get_all_cargo_names_for_vcn(vcn_id))

# Get cargo names for a specific VCN (for stowage plan dropdown)
@bp.route('/api/module/VCN01/cargo_names/<int:vcn_id>')
@login_required
def get_cargo_names(vcn_id):
    return jsonify(model.get_cargo_names_for_vcn(vcn_id))

# Stowage Plan endpoints
@bp.route('/api/module/VCN01/stowage/<int:vcn_id>')
@login_required
def get_stowage(vcn_id):
    return jsonify(model.get_stowage_plan(vcn_id))

@bp.route('/api/module/VCN01/stowage/save', methods=['POST'])
@login_required
def save_stowage():
    perms = get_perms()
    if not perms.get('can_add') and not perms.get('can_edit'):
        return jsonify({'error': 'No permission'}), 403
    row_id, error = model.save_stowage_plan(request.json)
    if error:
        return jsonify({'success': False, 'error': error}), 400
    return jsonify({'success': True, 'id': row_id})

@bp.route('/api/module/VCN01/stowage/delete', methods=['POST'])
@login_required
def delete_stowage():
    perms = get_perms()
    if not perms.get('can_delete'):
        return jsonify({'error': 'No permission to delete'}), 403
    model.delete_stowage_plan(request.json.get('id'))
    return jsonify({'success': True})

@bp.route('/api/module/VCN01/export_loading_totals/<int:vcn_id>')
@login_required
def get_export_loading_totals(vcn_id):
    return jsonify(model.get_export_loading_totals(vcn_id))

@bp.route('/api/module/VCN01/stowage/total/<int:vcn_id>')
@login_required
def get_stowage_total(vcn_id):
    # Check operation_type to determine which cargo total to use
    conn = model.get_db()
    cur = model.get_cursor(conn)
    cur.execute('SELECT operation_type FROM vcn_header WHERE id=%s', (vcn_id,))
    row = cur.fetchone()
    conn.close()
    op_type = row['operation_type'] if row else None

    if op_type == 'Export':
        cargo_total = model.get_export_cargo_total_quantity(vcn_id)
    else:
        cargo_total = model.get_cargo_total_quantity(vcn_id)

    return jsonify({
        'stowage_total': model.get_stowage_total_quantity(vcn_id),
        'igm_total': cargo_total
    })

# Hold Completion (read-only view from LDUD data)
@bp.route('/api/module/VCN01/hold_completion/<int:vcn_id>')
@login_required
def get_hold_completion(vcn_id):
    return jsonify(model.get_hold_completion_by_vcn(vcn_id))

@bp.route('/api/module/VCN01/vessel_holds/<int:vcn_id>')
@login_required
def get_vessel_holds(vcn_id):
    return jsonify({'no_of_holds': model.get_vessel_holds(vcn_id)})
