from functools import wraps

from flask import render_template, session, redirect, url_for

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


def get_perms():
    if session.get('is_admin'):
        return {'can_read': 1, 'can_add': 1, 'can_edit': 1, 'can_delete': 1}
    return get_user_permissions(session.get('user_id'), 'RP03')


@bp.route('/module/RP03/')
@login_required
def index():
    perms = get_perms()
    if not perms.get('can_read'):
        return render_template('no_access.html'), 403
    return render_template('rp03.html', username=session.get('username'),
                           permissions=perms, board=model.board())
