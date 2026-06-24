from flask import render_template, session, redirect, url_for
from functools import wraps
from . import bp
from .vessel_sof import views as _vessel_sof_views  # noqa: registers vessel-sof routes on bp
from .mbc_sof import views as _mbc_sof_views        # noqa: registers mbc-sof routes on bp
from .mbc_master import views as _mbc_master_views  # noqa: registers mbc-master routes on bp
from .mbc_tat    import views as _mbc_tat_views     # noqa: registers mbc-tat routes on bp
from .vessel_discharged import views as _vessel_discharged_views  # noqa: registers vessel-discharged routes on bp
from .custom_report    import views as _custom_report_views       # noqa: registers custom-report routes on bp
from .dashboard        import views as _dashboard_views           # noqa: registers dashboard routes on bp
from .daily_ops         import views as _daily_ops_views        # noqa: registers daily-ops routes on bp
from .shift_report      import views as _shift_report_views    # noqa: registers shift-report routes on bp
from .barge_report      import views as _barge_report_views    # noqa: registers barge-report routes on bp
from .mv_mbc_month_report import views as _mv_monthly_report_views  # noqa: registers mv-monthly-report routes on bp
from .live_dashboard    import views as _live_dashboard_views  # noqa: registers live-dashboard routes on bp
from .port_map          import views as _port_map_views        # noqa: registers port-map routes on bp
from .daily_barge_report import views as _mv_barge_report_views  # noqa: registers mv-barge-report routes on bp
from .cargo_report import views as _cargo_report_views
from .historical_data import views as _historical_data_views  # noqa: registers historical-data routes on bp
from .Barge_Position_Report  import views as _barge_position_views

from .report_24_hours import views as _report_24_hours_views
# from .daily_progress_report import views as _daily_progress_report_views

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@bp.route('/module/RP01/')
@login_required
def index():
    return render_template('rp01.html', username=session.get('username'))
