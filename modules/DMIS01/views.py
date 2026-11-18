from flask import Blueprint, render_template, request, jsonify, session, send_file
from functools import wraps
from database import get_user_permissions
from . import model
import io
import json

bp = Blueprint('DMIS01', __name__, template_folder='.')
MODULE_CODE = 'DMIS01'

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
    if not perms:
        return {'can_read': 1, 'can_add': 1, 'can_edit': 1, 'can_delete': 1}
    return perms

@bp.route('/module/DMIS01/')
def view():
    if 'user_id' not in session and 'username' not in session:
        return 'Unauthorized', 401
    perms = get_perms()
    if not perms.get('can_read'):
        return render_template('no_access.html'), 403
    return render_template('dmis01.html', permissions=perms)

# ── Masters ──────────────────────────────────────────────────────────────────
@bp.route('/api/module/DMIS01/equipments', methods=['GET'])
@login_required
def get_equipments_route():
    try:
        items = model.get_equipments()
        return jsonify(items)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/heme', methods=['GET'])
@login_required
def get_heme_route():
    try:
        items = model.get_heme_list()
        return jsonify(items)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/heme/save', methods=['POST'])
@login_required
def save_heme_route():
    try:
        perms = get_perms()
        if not perms.get('can_add') and not perms.get('can_edit'):
            return jsonify({'error': 'Permission denied'}), 403
        data = request.json or {}
        if not data.get('number') or not data.get('type'):
            return jsonify({'error': 'Type and Number are required'}), 400
        if not data.get('created_by'):
            data['created_by'] = session.get('username') or 'admin'
        saved_id = model.save_heme(data)
        return jsonify({'success': True, 'id': saved_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/heme/delete', methods=['POST'])
@login_required
def delete_heme_route():
    try:
        perms = get_perms()
        if not perms.get('can_delete'):
            return jsonify({'error': 'Permission denied'}), 403
        data = request.json or {}
        rid = data.get('id')
        if not rid:
            return jsonify({'error': 'Missing ID'}), 400
        model.delete_heme(rid)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Tab 1: Diesel Consumption Endpoints ──────────────────────────────────────
@bp.route('/api/module/DMIS01/consumption', methods=['GET'])
@login_required
def get_consumption_route():
    try:
        equipment = request.args.get('equipment', default='', type=str).strip()
        page = request.args.get('page', default=1, type=int)
        size = request.args.get('size', default=20, type=int)
        filters_raw = request.args.get('filters', default=None)
        filters = json.loads(filters_raw) if filters_raw else None
        data, total = model.get_consumption(equipment=equipment or None, page=page, size=size, filters=filters)
        last_page = max(1, (total + size - 1) // size) if size else 1
        return jsonify({'data': data, 'total': total, 'last_page': last_page})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/latest_reading', methods=['GET'])
@login_required
def get_latest_reading_route():
    try:
        equipment = request.args.get('equipment', default='', type=str).strip()
        reading = model.get_latest_end_reading(equipment)
        return jsonify({'end_reading': reading})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/consumption/save', methods=['POST'])
@login_required
def save_consumption_route():
    try:
        perms = get_perms()
        if not perms.get('can_add') and not perms.get('can_edit'):
            return jsonify({'error': 'Permission denied'}), 403
            
        data = request.json or {}
        entry_date = str(data.get('entry_date') or data.get('date') or '').strip()
        equipment = str(data.get('equipment') or data.get('equipment_name') or '').strip()
        if not entry_date:
            return jsonify({'error': 'Date is required'}), 400
        if not equipment:
            return jsonify({'error': 'Equipment is required. Please select an Equipment.'}), 400
            
        data['entry_date'] = entry_date
        data['equipment'] = equipment
        if not data.get('created_by'):
            data['created_by'] = session.get('username') or 'admin'
            
        saved_id = model.save_consumption(data)
        return jsonify({'success': True, 'id': saved_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/consumption/delete', methods=['POST'])
@login_required
def delete_consumption_route():
    try:
        perms = get_perms()
        if not perms.get('can_delete'):
            return jsonify({'error': 'Permission denied'}), 403
            
        data = request.json or {}
        ids = data.get('ids') or ([data.get('id')] if data.get('id') else [])
        if not ids:
            return jsonify({'error': 'Missing ID'}), 400
            
        for rid in ids:
            model.delete_consumption(rid)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Tab 2: Diesel Received Endpoints ─────────────────────────────────────────
@bp.route('/api/module/DMIS01/received', methods=['GET'])
@login_required
def get_received_route():
    try:
        month = request.args.get('month', default='', type=str).strip()
        page = request.args.get('page', default=1, type=int)
        size = request.args.get('size', default=20, type=int)
        filters_raw = request.args.get('filters', default=None)
        filters = json.loads(filters_raw) if filters_raw else None
        data, total = model.get_received(month=month or None, page=page, size=size, filters=filters)
        last_page = max(1, (total + size - 1) // size) if size else 1
        return jsonify({'data': data, 'total': total, 'last_page': last_page})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/received/save', methods=['POST'])
@login_required
def save_received_route():
    try:
        perms = get_perms()
        if not perms.get('can_add') and not perms.get('can_edit'):
            return jsonify({'error': 'Permission denied'}), 403
            
        data = request.json or {}
        if not data.get('received_date'):
            return jsonify({'error': 'Received Date is required'}), 400
            
        if not data.get('created_by'):
            data['created_by'] = session.get('username') or 'admin'
            
        saved_id = model.save_received(data)
        return jsonify({'success': True, 'id': saved_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/received/delete', methods=['POST'])
@login_required
def delete_received_route():
    try:
        perms = get_perms()
        if not perms.get('can_delete'):
            return jsonify({'error': 'Permission denied'}), 403
            
        data = request.json or {}
        ids = data.get('ids') or ([data.get('id')] if data.get('id') else [])
        if not ids:
            return jsonify({'error': 'Missing ID'}), 400
            
        for rid in ids:
            model.delete_received(rid)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Tab 3: Tank Stock Endpoints ──────────────────────────────────────────────
@bp.route('/api/module/DMIS01/stock', methods=['GET'])
@bp.route('/api/module/DMIS01/tank_stock', methods=['GET'])
@login_required
def get_tank_stock_route():
    try:
        equipment = request.args.get('equipment', default='', type=str).strip()
        page = request.args.get('page', default=1, type=int)
        size = request.args.get('size', default=20, type=int)
        filters_raw = request.args.get('filters', default=None)
        filters = json.loads(filters_raw) if filters_raw else None
        data, total = model.get_tank_stock(equipment=equipment or None, page=page, size=size, filters=filters)
        last_page = max(1, (total + size - 1) // size) if size else 1
        return jsonify({'data': data, 'total': total, 'last_page': last_page})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/stock/save', methods=['POST'])
@bp.route('/api/module/DMIS01/tank_stock/save', methods=['POST'])
@login_required
def save_tank_stock_route():
    try:
        perms = get_perms()
        if not perms.get('can_add') and not perms.get('can_edit'):
            return jsonify({'error': 'Permission denied'}), 403
            
        data = request.json or {}
        stock_date = str(data.get('stock_date') or data.get('stock_month') or data.get('date') or data.get('entry_date') or '').strip()
        equipment = str(data.get('equipment') or data.get('equipment_name') or '').strip()
        if not stock_date:
            return jsonify({'error': 'Date is required'}), 400
        if not equipment:
            return jsonify({'error': 'Equipment is required. Please select an Equipment.'}), 400
            
        data['stock_date'] = stock_date
        data['equipment'] = equipment
        if not data.get('created_by'):
            data['created_by'] = session.get('username') or 'admin'
            
        saved_id = model.save_tank_stock(data)
        return jsonify({'success': True, 'id': saved_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/stock/delete', methods=['POST'])
@bp.route('/api/module/DMIS01/tank_stock/delete', methods=['POST'])
@login_required
def delete_tank_stock_route():
    try:
        perms = get_perms()
        if not perms.get('can_delete'):
            return jsonify({'error': 'Permission denied'}), 403
            
        data = request.json or {}
        ids = data.get('ids') or ([data.get('id')] if data.get('id') else [])
        if not ids:
            return jsonify({'error': 'Missing ID'}), 400
            
        for rid in ids:
            model.delete_tank_stock(rid)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Summary & Multi-Sheet Excel Export ───────────────────────────────────────
@bp.route('/api/module/DMIS01/summary', methods=['GET'])
@login_required
def get_summary_route():
    try:
        month = request.args.get('month', default='', type=str).strip()
        summary = model.get_mis_summary(month=month or None)
        return jsonify(summary)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@bp.route('/api/module/DMIS01/export', methods=['GET'])
@login_required
def export_excel():
    try:
        import openpyxl
        equipment = request.args.get('equipment', default='', type=str).strip()
        
        wb = openpyxl.Workbook()
        
        # 1. Diesel Consumption Sheet
        ws1 = wb.active
        ws1.title = "Diesel Consumption"
        ws1.append(["Date", "Equipment", "Start Reading", "End Reading", "Difference", "Diesel Filled (Litres)", "Rate", "Remarks", "Created By"])
        cons_data, _ = model.get_consumption(equipment=equipment or None, page=None, size=None)
        for r in cons_data:
            ws1.append([
                r.get('entry_date', ''),
                r.get('equipment', ''),
                r.get('start_reading', ''),
                r.get('end_reading', ''),
                r.get('difference', ''),
                r.get('diesel_filled', ''),
                r.get('rate', ''),
                r.get('remarks', ''),
                r.get('created_by', '')
            ])
            
        # 2. Diesel Received Sheet
        ws2 = wb.create_sheet(title="Diesel Received")
        ws2.append(["Date", "Diesel Received (Litres)", "Created By"])
        recd_data, _ = model.get_received(page=None, size=None)
        for r in recd_data:
            ws2.append([
                r.get('received_date', ''),
                r.get('diesel_received', ''),
                r.get('created_by', '')
            ])
            
        # 3. Tank Stock Sheet
        ws3 = wb.create_sheet(title="Tank Stock")
        ws3.append(["Date", "Equipment", "Month-start Tank Stock (Litres)", "Created By"])
        stock_data, _ = model.get_tank_stock(equipment=equipment or None, page=None, size=None)
        for r in stock_data:
            ws3.append([
                r.get('stock_date', ''),
                r.get('equipment', ''),
                r.get('month_start_tank_stock', ''),
                r.get('created_by', '')
            ])
            
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        filename = "Diesel_MIS_Report.xlsx"
        return send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500
