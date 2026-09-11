from flask import Blueprint

MODULE_INFO = {
    'code': 'RP03',
    'name': 'Arrival Planner'
}

bp = Blueprint('RP03', __name__, template_folder='.')

from . import views
