from flask import Blueprint, render_template, request, jsonify, session, send_file, redirect
from functools import wraps
import io
from . import model
from database import get_user_permissions

bp = Blueprint('HEM01', __name__, template_folder='.')
MODULE_CODE = 'HEM01'


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session and 'username' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def get_perms():
    if session.get('is_admin'):
        return {'can_read': 1, 'can_add': 1, 'can_edit': 1, 'can_delete': 1}
    perms = get_user_permissions(session.get('user_id'), MODULE_CODE)
    if not perms or not perms.get('can_read'):
        perms = get_user_permissions(session.get('user_id'), 'HEME01')
    if not perms or not perms.get('can_read'):
        return {'can_read': 1, 'can_add': 1, 'can_edit': 1, 'can_delete': 1}
    return perms


@bp.route('/module/HEM01/')
def view():
    if 'user_id' not in session and 'username' not in session:
        return 'Unauthorized', 401
    perms = get_perms()
    if not perms.get('can_read'):
        return render_template('no_access.html'), 403
    return render_template('hem01.html', permissions=perms)


@bp.route('/module/HEME01/')
def view_alias():
    return redirect('/module/HEM01/')


@bp.route('/api/module/HEM01/data', methods=['GET'])
@bp.route('/api/module/HEME01/data', methods=['GET'])
@login_required
def get_data():
    try:
        page = request.args.get('page', default=1, type=int)
        size = request.args.get('size', default=20, type=int)
        search = request.args.get('search', default='', type=str).strip()
        data, total = model.get_data(page=page, size=size, search=search)
        last_page = max(1, (total + size - 1) // size) if size else 1
        return jsonify({'data': data, 'total': total, 'last_page': last_page})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/module/HEM01/all', methods=['GET'])
@bp.route('/api/module/HEME01/all', methods=['GET'])
@login_required
def get_all():
    try:
        items = model.get_all()
        return jsonify(items)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/module/HEM01/save', methods=['POST'])
@bp.route('/api/module/HEME01/save', methods=['POST'])
@login_required
def save():
    try:
        perms = get_perms()
        if not perms.get('can_add') and not perms.get('can_edit'):
            return jsonify({'error': 'Permission denied'}), 403

        data = request.json or {}
        eq_type = str(data.get('type') or '').strip()
        number = str(data.get('number') or '').strip()

        if not eq_type:
            return jsonify({'error': 'Type is required'}), 400
        if not number:
            return jsonify({'error': 'Number is required'}), 400

        data['type'] = eq_type
        data['number'] = number
        if not data.get('created_by'):
            data['created_by'] = session.get('username') or 'admin'

        saved_id = model.save_data(data)
        return jsonify({'success': True, 'id': saved_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/module/HEM01/delete', methods=['POST'])
@bp.route('/api/module/HEME01/delete', methods=['POST'])
@login_required
def delete():
    try:
        perms = get_perms()
        if not perms.get('can_delete'):
            return jsonify({'error': 'Permission denied'}), 403

        data = request.json or {}
        ids = data.get('ids') or ([data.get('id')] if data.get('id') else [])
        if not ids:
            return jsonify({'error': 'Missing ID'}), 400

        for rid in ids:
            model.delete_data(rid)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/api/module/HEM01/export', methods=['GET'])
@bp.route('/api/module/HEME01/export', methods=['GET'])
@login_required
def export_excel():
    try:
        import openpyxl
        data, _ = model.get_data(page=1, size=100000)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "HEME Master"
        ws.append(["ID", "Type", "Model", "Number", "Created By"])
        for r in data:
            ws.append([
                r.get('id', ''),
                r.get('type', ''),
                r.get('model', ''),
                r.get('number', ''),
                r.get('created_by', '')
            ])
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="HEME_Master.xlsx"
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500
