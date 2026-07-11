from functools import wraps
from flask import render_template, request, jsonify, session, redirect, url_for, Response

from database import get_user_permissions
from . import bp
from . import model


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def _can_upload():
    if session.get('is_admin'):
        return True
    perms = get_user_permissions(session['user_id'], 'RP02')
    return bool(perms['can_add'] or perms['can_edit'])


def upload_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if not _can_upload():
            return jsonify({'error': 'No permission to modify bill master data'}), 403
        return f(*args, **kwargs)
    return decorated


@bp.route('/module/RP02/')
@login_required
def index():
    return render_template('rp02.html', username=session.get('username'),
                           can_upload=_can_upload())


@bp.route('/module/RP02/bill-master/')
@login_required
def bill_master_index():
    return render_template('bill_master.html', username=session.get('username'),
                           status=model.get_status(), can_upload=_can_upload())


@bp.route('/module/RP02/bill-master-report/')
@login_required
def bill_master_report_index():
    return render_template('bill_master_report.html', username=session.get('username'))


@bp.route('/api/module/RP02/bill-master-report/data')
@login_required
def bill_master_report_data():
    return jsonify({'data': model.get_bill_master_report()})


@bp.route('/api/module/RP02/bill-master/template')
@login_required
def bill_master_template():
    return Response(
        model.build_template_csv(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="RP02_bill_master_template.csv"'},
    )


@bp.route('/api/module/RP02/bill-master/preview', methods=['POST'])
@upload_required
def bill_master_preview():
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file provided'}), 400
    rows, errors = model.parse_upload(f)
    customers = model.get_customer_master()
    recon = model.reconcile(rows, customers)
    years = sorted({r['financial_year'] for r in rows if r.get('financial_year')})
    opts = {col: customers for col, info in recon.items() if info['unknown']}
    return jsonify({'total_rows': len(rows), 'format_errors': errors,
                    'years': years,
                    'reconciliation': recon,
                    'master_options': opts,
                    'addable_columns': ['customer_name']})


@bp.route('/api/module/RP02/bill-master/apply', methods=['POST'])
@upload_required
def bill_master_apply():
    import json as _json
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file provided'}), 400
    try:
        resolutions = _json.loads(request.form.get('resolutions') or '{}')
    except (ValueError, TypeError):
        resolutions = {}

    rows, errors = model.parse_upload(f)
    if errors:
        return jsonify({'error': 'Fix format errors before applying',
                        'format_errors': errors}), 400

    # 1) Add-to-master actions (customer master only).
    added, add_errors = [], []
    for value, res in (resolutions.get('customer_name') or {}).items():
        if isinstance(res, dict) and res.get('action') == 'add':
            try:
                if model.add_customer(value):
                    added.append({'column': 'customer_name', 'value': value})
            except Exception as e:  # noqa: BLE001 — surface, don't abort
                add_errors.append({'column': 'customer_name', 'value': value, 'error': str(e)})

    # 2) Replace actions rewrite the parsed rows, then per-FY replace insert.
    rows = model.apply_resolutions(rows, resolutions)
    inserted, years = model.replace_years(rows, session.get('user_id'))
    return jsonify({'inserted': inserted, 'years': years,
                    'added_to_master': added, 'add_errors': add_errors})


@bp.route('/api/module/RP02/bill-master/rows')
@login_required
def bill_master_rows():
    import json as _json
    try:
        page = int(request.args.get('page', 1))
        size = int(request.args.get('size', 50))
    except (TypeError, ValueError):
        page, size = 1, 50
    try:
        filters = _json.loads(request.args.get('colfilters') or '[]')
        if not isinstance(filters, list):
            filters = []
    except (ValueError, TypeError):
        filters = []
    rows, total = model.get_rows(page, size, filters)
    return jsonify({'data': rows, 'last_page': max(1, (total + size - 1) // size), 'total': total})


@bp.route('/api/module/RP02/bill-master/row/update', methods=['POST'])
@upload_required
def bill_master_row_update():
    data = request.json or {}
    if not data.get('id'):
        return jsonify({'error': 'Missing id'}), 400
    res = model.update_row(data['id'], data)
    if res.get('error'):
        return jsonify(res), 400
    return jsonify(res)


@bp.route('/api/module/RP02/bill-master/row/delete', methods=['POST'])
@upload_required
def bill_master_row_delete():
    data = request.json or {}
    if not data.get('id'):
        return jsonify({'error': 'Missing id'}), 400
    model.delete_row(data['id'])
    return jsonify({'success': True})
