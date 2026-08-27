"""Forecast model checks — calibration and seasonality recovery.

These exercise the pure numpy core against synthetic series (no DB), so they
run anywhere. The one DB-backed test skips when the table isn't seeded.
"""
from datetime import date, timedelta

import numpy as np
import pytest

from modules.RP01.RP01.port_overview import forecast as fc


def _series(start, n, fn):
    dates = [start + timedelta(days=i) for i in range(n)]
    return dates, np.array([fn(d, i) for i, d in enumerate(dates)], dtype=float)


def _patch(monkeypatch, dates, y):
    monkeypatch.setattr(fc, 'load_series', lambda: (dates, y))


def test_month_index_recovers_a_known_seasonal_shape():
    """A series built with a July dip and a March push should measure as one."""
    true = {m: 1.0 for m in range(1, 13)}
    true[7], true[3] = 0.80, 1.30
    dates, y = _series(date(2018, 4, 1), 365 * 6,
                       lambda d, i: 50000 * true[d.month])

    idx = fc.month_index(dates, y)
    assert idx[7] / idx[1] == pytest.approx(0.80, abs=0.02)
    assert idx[3] / idx[1] == pytest.approx(1.30, abs=0.02)
    assert idx[1:].mean() == pytest.approx(1.0, abs=1e-6)


def test_level_ignores_old_years_and_tracks_the_recent_step_up(monkeypatch):
    """Throughput doubled mid-series; the forecast must use the new level."""
    dates, y = _series(date(2018, 4, 1), 365 * 6,
                       lambda d, i: 25000.0 if i < 365 * 5 else 50000.0)
    _patch(monkeypatch, dates, y)
    out = fc.forecast('fy', today=dates[-1], seed=1)

    assert out['daily_rate'] == pytest.approx(50000, rel=0.02)


def test_probability_is_calibrated_at_the_median_and_the_tails(monkeypatch):
    """P(hit) ~= 0.5 at the expected total, and pinned at the extremes."""
    rng = np.random.default_rng(0)
    noise = rng.normal(1.0, 0.15, 365 * 4)
    dates, y = _series(date(2020, 4, 1), 365 * 4,
                       lambda d, i: 50000 * noise[i])
    _patch(monkeypatch, dates, y)

    today = date(2023, 10, 1)          # ~half of FY2023-24 still to run
    mid = fc.forecast('fy', target=None, today=today, seed=7)
    expected = mid['expected']

    assert fc.forecast('fy', expected, today, seed=7)['p_hit'] == pytest.approx(0.5, abs=0.06)
    assert fc.forecast('fy', expected * 0.5, today, seed=7)['p_hit'] > 0.99
    assert fc.forecast('fy', expected * 1.5, today, seed=7)['p_hit'] < 0.01


def test_optimistic_expected_pessimistic_are_ordered(monkeypatch):
    rng = np.random.default_rng(3)
    dates, y = _series(date(2020, 4, 1), 365 * 4,
                       lambda d, i: 50000 * rng.normal(1.0, 0.2))
    _patch(monkeypatch, dates, y)

    out = fc.forecast('fy', today=date(2023, 8, 1), seed=5)
    assert out['pessimistic'] < out['expected'] < out['optimistic']


def test_block_bootstrap_widens_the_fan_versus_iid_draws(monkeypatch):
    """The reason for BLOCK: autocorrelated history must widen the band.

    Level drift is pinned to zero here, otherwise it dominates the variance and
    swamps the effect being measured.
    """
    rng = np.random.default_rng(11)
    # AR(1) noise with strong persistence, like real weather/breakdown streaks.
    e, vals = 0.0, []
    for _ in range(365 * 4):
        e = 0.7 * e + rng.normal(0, 0.1)
        vals.append(50000 * (1 + e))
    dates, y = _series(date(2020, 4, 1), 365 * 4, lambda d, i: vals[i])
    _patch(monkeypatch, dates, y)
    monkeypatch.setattr(fc, '_level_sigma', lambda *a, **k: 0.0)

    wide = fc.forecast('fy', today=date(2023, 10, 1), seed=2)
    monkeypatch.setattr(fc, 'BLOCK', 1)
    narrow = fc.forecast('fy', today=date(2023, 10, 1), seed=2)

    assert (wide['optimistic'] - wide['pessimistic']) > \
           (narrow['optimistic'] - narrow['pessimistic']) * 1.5


def test_level_drift_is_what_keeps_the_long_horizon_fan_open(monkeypatch):
    """Without drift the daily noise averages away and every P(hit) pins at 0/1."""
    rng = np.random.default_rng(4)
    dates, y = _series(date(2018, 4, 1), 365 * 6,
                       lambda d, i: 50000 * rng.normal(1.0, 0.2))
    _patch(monkeypatch, dates, y)
    today = date(2023, 7, 31)

    with_drift = fc.forecast('fy', today=today, seed=3)
    monkeypatch.setattr(fc, '_level_sigma', lambda *a, **k: 0.0)
    without = fc.forecast('fy', today=today, seed=3)

    span = lambda o: (o['optimistic'] - o['pessimistic']) / o['expected']
    assert span(without) < 0.05, 'daily noise alone should collapse the fan'
    assert span(with_drift) > span(without) * 3


def test_month_scope_covers_exactly_the_calendar_month(monkeypatch):
    dates, y = _series(date(2020, 4, 1), 365 * 4, lambda d, i: 50000.0)
    _patch(monkeypatch, dates, y)

    out = fc.forecast('month', today=date(2023, 11, 10), seed=1)
    assert out['period'] == {'start': '2023-11-01', 'end': '2023-11-30'}
    assert out['days_left'] == 20
    assert out['achieved'] == pytest.approx(50000 * 10)


def test_no_days_left_is_deterministic(monkeypatch):
    # 1461 days = 2020-04-01 .. 2024-03-31 inclusive (two leap years), so the
    # series ends exactly on the last day of the month being scored.
    dates, y = _series(date(2020, 4, 1), 1461, lambda d, i: 50000.0)
    _patch(monkeypatch, dates, y)
    assert dates[-1] == date(2024, 3, 31)

    out = fc.forecast('month', target=1, today=date(2024, 3, 31), seed=1)
    assert out['days_left'] == 0
    assert out['p_hit'] == 1.0
    assert out['expected'] == out['optimistic'] == out['pessimistic']


def test_short_history_reports_unavailable_instead_of_guessing(monkeypatch):
    dates, y = _series(date(2024, 1, 1), 20, lambda d, i: 50000.0)
    _patch(monkeypatch, dates, y)

    assert fc.forecast('fy', today=date(2024, 1, 20))['available'] is False


def test_suggested_months_sum_exactly_to_the_anchor(monkeypatch):
    """Rounding must not leak: months add to the FY total, categories to the month."""
    dates, y = _series(date(2020, 4, 1), 365 * 5, lambda d, i: 50000.0)
    _patch(monkeypatch, dates, y)
    monkeypatch.setattr(fc, 'category_mix', lambda f: {'IBRM': .6, 'CBRM': .25,
                                                       'FLUXES': .13, 'CLINKER': .02,
                                                       'SLAG': 0.0})
    s = fc.suggest(2024, today=date(2024, 8, 1))

    assert s['available']
    assert [a['key'] for a in s['anchors']] == [
        'last_fy', 'trend', 'forecast_p10', 'forecast_p50'], 'anchor order must be stable'
    for a in s['anchors']:
        assert sum(m['base_target'] for m in a['months']) == a['total'], a['key']
        for m in a['months']:
            assert sum(m['categories'].values()) == m['base_target'], (a['key'], m['month_num'])


def test_suggestion_carries_the_monsoon_shape(monkeypatch):
    """July must be given a smaller share of the year than March."""
    true = {m: 1.0 for m in range(1, 13)}
    true[7], true[3] = 0.80, 1.30
    dates, y = _series(date(2018, 4, 1), 365 * 6,
                       lambda d, i: 50000 * true[d.month])
    _patch(monkeypatch, dates, y)
    monkeypatch.setattr(fc, 'category_mix', lambda f: {'IBRM': 1.0})
    s = fc.suggest(2024, today=date(2024, 8, 1))

    jul = s['month_share']['7']
    mar = s['month_share']['3']
    assert jul < 1 / 12 < mar
    assert mar / jul == pytest.approx(1.30 / 0.80, rel=0.05)
    assert sum(s['month_share'].values()) == pytest.approx(1.0)


def test_trend_anchor_reads_a_plateau_as_a_plateau(monkeypatch):
    """Flat recent years must not be extrapolated into growth."""
    dates, y = _series(date(2018, 4, 1), 365 * 6, lambda d, i: 50000.0)
    _patch(monkeypatch, dates, y)
    monkeypatch.setattr(fc, 'category_mix', lambda f: {'IBRM': 1.0})
    s = fc.suggest(2024, today=date(2024, 8, 1))

    by = {a['key']: a for a in s['anchors']}
    assert by['trend']['total'] == pytest.approx(by['last_fy']['total'], rel=0.01)


def test_suggestion_needs_a_complete_prior_year(monkeypatch):
    dates, y = _series(date(2024, 4, 1), 320, lambda d, i: 50000.0)
    _patch(monkeypatch, dates, y)
    assert fc.suggest(2024, today=date(2025, 2, 1))['available'] is False


def test_seeded_history_is_contiguous_and_forecasts():
    """Against the real seeded table, when there is one."""
    dates, y = fc.load_series()
    if len(y) < fc.LEVEL_WINDOW * 2:
        pytest.skip('rp01_daily_throughput not seeded')

    assert (dates[-1] - dates[0]).days + 1 == len(dates), 'gaps in seeded series'
    assert len(set(dates)) == len(dates), 'duplicate dates in seeded series'

    out = fc.forecast('fy', target=25_000_000, seed=1)
    assert out['available'] and 0.0 <= out['p_hit'] <= 1.0
    assert out['pessimistic'] < out['expected'] < out['optimistic']
