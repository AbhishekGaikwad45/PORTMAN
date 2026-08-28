"""Day-count check for the throughput forecast.

The bug this pins: the day you are standing on is still to be worked, so it
belongs to the forecast, not to history. On 28 Aug the month has 4 days left
(28-31), not 3, and the FY has 28 Aug -> 31 Mar inclusive.
"""
import sys
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, '.')
from modules.RP01.RP01.port_overview import forecast as fc


def _fake_series(end, n=900, per_day=1000.0):
    dates = [end - timedelta(days=i) for i in range(n)][::-1]
    return dates, np.full(n, per_day)


def _run(scope, today, data_end=None, partial=0.0):
    dates, y = _fake_series(data_end or today)
    if partial and dates[-1] == today:
        y[-1] = partial
    fc.load_series = lambda: (dates, y)
    return fc.forecast(scope, target=None, today=today, seed=0)


def main():
    today = date(2026, 8, 28)

    m = _run('month', today)
    assert m['days_left'] == 4, m['days_left']            # 28,29,30,31
    assert m['as_of'] == '2026-08-27', m['as_of']

    f = _run('fy', today)
    assert f['days_left'] == (date(2027, 3, 31) - today).days + 1 == 216, f['days_left']

    # Today's part-day total is reported, but kept out of achieved and out of
    # the 60-day rate (which would otherwise be dragged down by it).
    p = _run('month', today, partial=50.0)
    assert p['partial_today'] == 50.0, p['partial_today']
    assert p['achieved'] == 27000.0, p['achieved']        # 1-27 Aug x 1000
    assert p['daily_rate'] == 1000.0, p['daily_rate']

    # Data lagging by two days: those days are unworked history, so they are
    # simulated too and the horizon grows past the calendar days remaining.
    lag = _run('month', today, data_end=today - timedelta(days=2))
    assert lag['days_left'] == 5, lag['days_left']        # 27..31
    assert lag['as_of'] == '2026-08-26', lag['as_of']

    # Daily bands line up 1:1 with the simulated days.
    for key in ('daily_p10', 'daily_p50', 'daily_p90'):
        assert len(m[key]) == m['days_left'], key
    assert m['daily_p10'][0]['d'] == '2026-08-28'
    assert len(m['daily_actual']) == 27
    print('ok')


if __name__ == '__main__':
    main()
