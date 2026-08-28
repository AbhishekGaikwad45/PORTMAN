"""Throughput forecast + probability of hitting the FY / month target.

Model, in one breath: daily rate = recent de-seasonalised level x month-of-year
index, simulated forward with residuals resampled in 7-day blocks. The block
matters — lag-1 autocorrelation of the daily residual is ~0.44 (bad weather and
breakdowns run in streaks), so drawing residuals independently would make the
fan far too narrow and the probability far too confident.

Only the *shape* of old years is reused, never their level: throughput stepped
from ~12.8M MT/yr (FY2018-21) to ~24M MT/yr (FY2023+), but the monsoon dip
(Jul index 0.82) and the March year-end push (1.28) have held throughout.

Day-of-week is deliberately not modelled — measured indices are 0.98-1.02
across all seven days, which is a 7-day port with nothing to learn.

History comes from rp01_daily_throughput (see seed_eq_wise.py); days after the
last seeded date come live from lueu_lines, so the forecast self-updates.
"""
import bisect
import calendar
from datetime import date, timedelta

import numpy as np

from database import get_db, get_cursor

N_SIMS = 5000
BLOCK = 7           # residual bootstrap block length, in days
LEVEL_WINDOW = 60   # trailing days defining "our current rate"
RATIO_POOL = 730    # how far back residuals are drawn from
MIN_FY_DAYS = 300   # a partial FY can't contribute a seasonal shape
DRIFT_STEP = 30     # horizon over which level drift is calibrated

# lueu_lines.entry_date is TEXT, and delay rows are saved with it blank — a bare
# `entry_date::date` therefore dies with "invalid input syntax for type date".
# Other queries in RP01 get away with the bare cast only because they also
# filter `quantity IS NOT NULL`, which happens to exclude those rows; anything
# aggregating every row (as the forecast does) must guard the cast itself.
# CASE is what makes it safe: it fixes evaluation order, where a WHERE filter
# alongside the cast does not.
LUEU_DATE = ("CASE WHEN entry_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "
             "THEN LEFT(entry_date, 10)::date END")


def load_series():
    """(dates, totals) — seeded history, extended with live lueu_lines days."""
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute("SELECT to_regclass('rp01_daily_throughput') AS t")
        if not cur.fetchone()['t']:
            return [], np.array([])

        cur.execute("SELECT entry_date, total FROM rp01_daily_throughput ORDER BY entry_date")
        rows = [(r['entry_date'], float(r['total'] or 0)) for r in cur.fetchall()]
        if not rows:
            return [], np.array([])

        # Days past the seeded history. Both live tables are consulted because
        # RP01 sources a FY's April from rp01_historical_lueu and May-onward
        # from lueu_lines (see views._fy_window) — reading only lueu_lines would
        # silently drop every future April. Where a date exists in both,
        # lueu_lines wins, so the two can never be double-counted.
        cur.execute(f"""
            WITH live AS (
                SELECT d, SUM(q) AS q FROM (
                    SELECT {LUEU_DATE} AS d, COALESCE(quantity, 0) AS q
                    FROM lueu_lines
                    WHERE is_deleted IS NOT TRUE
                ) s
                WHERE d > %(after)s
                GROUP BY d
            ), hist AS (
                SELECT entry_date AS d, SUM(COALESCE(quantity, 0)) AS q
                FROM rp01_historical_lueu
                WHERE entry_date > %(after)s
                GROUP BY 1
            )
            SELECT COALESCE(l.d, h.d) AS d, COALESCE(l.q, h.q) AS q
            FROM live l FULL OUTER JOIN hist h ON l.d = h.d
            ORDER BY 1
        """, {'after': rows[-1][0]})
        rows += [(r['d'], float(r['q'] or 0)) for r in cur.fetchall()]
    finally:
        conn.close()
    return [r[0] for r in rows], np.array([r[1] for r in rows], dtype=float)


def month_index(dates, y):
    """12 multiplicative month-of-year factors, normalised to mean 1.

    Each day is divided by its own FY's median before the per-month medians are
    taken, so a high-volume year and a low-volume year contribute shape equally.
    """
    idx = np.ones(13)
    if len(y) < MIN_FY_DAYS:
        return idx

    fy = np.array([d.year if d.month >= 4 else d.year - 1 for d in dates])
    months = np.array([d.month for d in dates])

    norm = np.full(len(y), np.nan)
    for f in np.unique(fy):
        m = fy == f
        med = np.median(y[m])
        if m.sum() >= MIN_FY_DAYS and med > 0:
            norm[m] = y[m] / med

    ok = ~np.isnan(norm)
    if ok.sum() < MIN_FY_DAYS:
        return idx
    for mo in range(1, 13):
        sel = ok & (months == mo)
        if sel.sum():
            idx[mo] = np.median(norm[sel])
    idx[1:] /= idx[1:].mean()
    return idx


def _levels(deseason):
    """Trailing de-seasonalised level, one per day after the warm-up window."""
    return np.array([np.median(deseason[i - LEVEL_WINDOW:i])
                     for i in range(LEVEL_WINDOW, len(deseason))])


def _ratios(deseason, levels):
    """Actual / expected, where expected = trailing level x month index."""
    obs = deseason[LEVEL_WINDOW:]
    ok = levels > 0
    return (obs[ok] / levels[ok])[-RATIO_POOL:]


def _level_sigma(levels, step=DRIFT_STEP):
    """Per-day sigma of a random walk in log(level).

    Calibrated from how far the trailing 60-day level has actually drifted over
    `step`-day horizons in this port's own history. Without this the simulation
    treats today's rate as a certainty and the daily noise averages away over a
    long horizon, collapsing the fan to a couple of percent and pinning every
    probability at 0 or 100.
    """
    lv = levels[levels > 0]
    if len(lv) < step * 3:
        return 0.0
    return float(np.std(np.log(lv[step:] / lv[:-step])) / np.sqrt(step))


def _simulate(level, midx, future_months, ratios, sigma, rng):
    """(N_SIMS, n_days) simulated daily throughput."""
    n = len(future_months)
    if n == 0:
        return np.zeros((N_SIMS, 0))

    base = level * midx[future_months]
    if len(ratios) < BLOCK * 2:
        return np.tile(base, (N_SIMS, 1))

    # Block bootstrap: pick start offsets, take BLOCK consecutive ratios from
    # each, trim to length. Preserves the day-to-day persistence iid draws lose.
    n_blocks = int(np.ceil(n / BLOCK))
    starts = rng.integers(0, len(ratios) - BLOCK, size=(N_SIMS, n_blocks))
    draws = ratios[(starts[:, :, None] + np.arange(BLOCK)).reshape(N_SIMS, -1)][:, :n]

    # Level drift: the dominant risk over a multi-month horizon.
    drift = np.exp(np.cumsum(rng.normal(0.0, sigma, size=(N_SIMS, n)), axis=1))
    return np.maximum(base * draws * drift, 0)


def period_bounds(scope, today):
    if scope == 'month':
        start = today.replace(day=1)
        return start, today.replace(day=calendar.monthrange(today.year, today.month)[1])
    fy_start = today.year if today.month >= 4 else today.year - 1
    return date(fy_start, 4, 1), date(fy_start + 1, 3, 31)


TARGET_CATEGORIES = ['IBRM', 'CBRM', 'FLUXES', 'CLINKER', 'SLAG']
TREND_YEARS = 3          # CAGR window for the trend anchor
TREND_CLAMP = (-.10, .15)


def fy_totals(dates, y):
    """{fy_start_year: total MT, complete FYs only}."""
    out = {}
    for d, v in zip(dates, y):
        out[d.year if d.month >= 4 else d.year - 1] = \
            out.get(d.year if d.month >= 4 else d.year - 1, 0.0) + float(v)
    counts = {}
    for d in dates:
        f = d.year if d.month >= 4 else d.year - 1
        counts[f] = counts.get(f, 0) + 1
    return {f: t for f, t in out.items() if counts[f] >= MIN_FY_DAYS}


def category_mix(fy_start_year):
    """Share of throughput per target category over one FY, from the live tables.

    The seeded Excel is equipment-wise and carries no cargo split at all, so the
    mix can only come from lueu_lines / rp01_historical_lueu. Returns {} when
    that FY has no cargo-tagged data.
    """
    start, end = date(fy_start_year, 4, 1), date(fy_start_year + 1, 3, 31)
    conn = get_db()
    cur = get_cursor(conn)
    try:
        cur.execute(f"""
            SELECT UPPER(TRIM(COALESCE(vc.cargo_type, 'OTHERS'))) AS ct,
                   SUM(COALESCE(q, 0)) AS q
            FROM (
                SELECT cargo_name, quantity AS q, {LUEU_DATE} AS d
                FROM lueu_lines WHERE is_deleted IS NOT TRUE
                UNION ALL
                SELECT cargo_name, quantity AS q, entry_date AS d
                FROM rp01_historical_lueu
            ) s
            LEFT JOIN vessel_cargo vc
                   ON LOWER(TRIM(vc.cargo_name)) = LOWER(TRIM(s.cargo_name))
            WHERE s.d BETWEEN %s AND %s
            GROUP BY 1
        """, (start, end))
        raw = {r['ct']: float(r['q'] or 0) for r in cur.fetchall()}
    finally:
        conn.close()

    # Renormalise over the five categories targets are actually set against —
    # OTHERS / FINISH GOODS never get a monthly target.
    keep = {c: raw.get(c, 0.0) for c in TARGET_CATEGORIES}
    tot = sum(keep.values())
    return {c: v / tot for c, v in keep.items()} if tot > 0 else {}


def month_weights(midx, fy_start_year):
    """Share of the FY total that each month should carry, monsoon included.

    Month index x that month's length, normalised — so July gets less both for
    being the monsoon trough and for whatever its day count is.
    """
    w = {}
    for m in list(range(4, 13)) + list(range(1, 4)):
        yr = fy_start_year if m >= 4 else fy_start_year + 1
        w[m] = midx[m] * calendar.monthrange(yr, m)[1]
    tot = sum(w.values())
    return {m: v / tot for m, v in w.items()}


def suggest(fy_start_year, today=None, seed=0):
    """Realistic FY targets, split by month (seasonality) and category (mix).

    Offers several anchors for the FY total rather than picking one — a target
    is a business commitment, and "last year again" vs "what we're on track for"
    are different decisions that only the user can make. Each anchor comes back
    as a ready-to-save 12-month grid.
    """
    dates, y = load_series()
    if len(y) < MIN_FY_DAYS:
        return {'available': False, 'reason': 'Not enough history to suggest targets.'}

    midx = month_weights(month_index(dates, y), fy_start_year)
    totals = fy_totals(dates, y)
    prior = sorted(f for f in totals if f < fy_start_year)
    if not prior:
        return {'available': False, 'reason': 'No complete prior financial year.'}

    last_fy = prior[-1]
    last_total = totals[last_fy]

    # Trend: CAGR over the last few complete years. A plateau then reads as a
    # plateau, where a single year-on-year jump would read as growth.
    span = min(TREND_YEARS, len(prior) - 1)
    if span >= 1 and totals[prior[-1 - span]] > 0:
        cagr = (last_total / totals[prior[-1 - span]]) ** (1 / span) - 1
    else:
        cagr = 0.0
    cagr = max(TREND_CLAMP[0], min(TREND_CLAMP[1], cagr))

    fc = forecast('fy', today=today, seed=seed)
    # A list, not a dict: Flask sorts JSON object keys, and these are meant to
    # read least-to-most ambitious.
    anchors = [
        {
            'key':   'last_fy',
            'label': f'Repeat FY{last_fy}-{last_fy + 1}',
            'total': round(last_total),
            'note':  'Last complete financial year, delivered again.',
        },
        {
            'key':   'trend',
            'label': f'Trend ({cagr * 100:+.1f}%/yr)',
            'total': round(last_total * (1 + cagr)),
            'note':  f'Last FY grown at the {span}-year CAGR.',
        },
    ]
    if fc.get('available'):
        anchors.append({
            'key':   'forecast_p10',
            'label': 'Committed floor (P10)',
            'total': round(fc['pessimistic']),
            'note':  'What this year delivers even on a bad run — beaten 9 years in 10.',
        })
        anchors.append({
            'key':   'forecast_p50',
            'label': 'On current form (P50)',
            'total': round(fc['expected']),
            'note':  'What this year is tracking towards — a 50/50 target.',
        })

    # Cargo mix: prefer the last complete FY, but walk back (and finally accept
    # the part-finished current FY) rather than return nothing — an empty mix
    # leaves every category blank, and base_target is derived from those.
    mix, mix_fy = {}, None
    for f in [last_fy, last_fy - 1, last_fy - 2, fy_start_year]:
        mix = category_mix(f)
        if mix:
            mix_fy = f
            break

    order = list(range(4, 13)) + list(range(1, 4))
    for a in anchors:
        months = []
        for m in order:
            base = round(a['total'] * midx[m])
            cats = {c: round(base * s) for c, s in mix.items()}
            if cats:      # categories must sum to base exactly — the UI adds them up
                top = max(cats, key=lambda c: cats[c])
                cats[top] += base - sum(cats.values())
            months.append({'month_num': m, 'base_target': base, 'categories': cats})
        # Same for the year: months must sum to the anchor, not to anchor ± rounding.
        drift = a['total'] - sum(x['base_target'] for x in months)
        if drift:
            last = months[-1]
            last['base_target'] += drift
            if last['categories']:
                top = max(last['categories'], key=lambda c: last['categories'][c])
                last['categories'][top] += drift
        a['months'] = months

    return {
        'available':    True,
        'fy_start':     fy_start_year,
        'anchors':      anchors,
        'default':      'trend',
        'mix':          {c: round(s, 4) for c, s in mix.items()},
        'mix_fy':       f'{mix_fy}-{mix_fy + 1}' if mix_fy is not None else None,
        'mix_is_partial': mix_fy == fy_start_year,
        'month_share':  {str(m): round(s, 4) for m, s in midx.items()},
        'history':      [{'fy': f'{f}-{f + 1}', 'total': round(totals[f])} for f in prior],
    }


_BACKTEST_CACHE = {}


def backtest(cutoff_month=7, cutoff_day=31):
    """Re-forecast each complete past FY from the same point in the year.

    Answers the only question an ops user really has about a forecast — "how
    wrong has this been before?" — using this port's own history. Cached on the
    last data date, since it only moves when history does.
    """
    dates, y = load_series()
    if not dates:
        return []
    key = (dates[-1], cutoff_month, cutoff_day)
    if key in _BACKTEST_CACHE:
        return _BACKTEST_CACHE[key]

    pos = {d: i for i, d in enumerate(dates)}
    rows = []
    for fy in range(dates[0].year, dates[-1].year + 1):
        cut, end = date(fy, cutoff_month, cutoff_day), date(fy + 1, 3, 31)
        if cut not in pos or end not in pos or pos[cut] < 400:
            continue
        n = pos[cut] + 1
        sub_d, sub_y = dates[:n], y[:n]

        real = globals()['load_series']
        globals()['load_series'] = lambda: (sub_d, sub_y)
        try:
            o = forecast('fy', today=cut, seed=1)
        finally:
            globals()['load_series'] = real
        if not o.get('available'):
            continue

        actual = float(sum(v for d, v in zip(dates, y) if date(fy, 4, 1) <= d <= end))
        if actual <= 0:
            continue
        rows.append({
            'fy':        f"{fy}-{fy + 1}",
            'predicted': o['expected'],
            'actual':    round(actual, 1),
            'error_pct': round((o['expected'] - actual) / actual * 100, 1),
            'in_band':   bool(o['pessimistic'] <= actual <= o['optimistic']),
        })

    _BACKTEST_CACHE[key] = rows
    return rows


def accuracy():
    """Headline accuracy line for the modal, or None if untestable."""
    rows = backtest()
    if not rows:
        return None
    errs = [abs(r['error_pct']) for r in rows]
    return {
        'years':    len(rows),
        'mae_pct':  round(sum(errs) / len(errs), 1),
        'bias_pct': round(sum(r['error_pct'] for r in rows) / len(rows), 1),
        'in_band':  sum(1 for r in rows if r['in_band']),
        'rows':     rows,
    }


def forecast(scope='fy', target=None, today=None, seed=None):
    """Forecast the period total and P(total >= target).

    `target` is passed in rather than read here, so this module stays unaware of
    the targets table (and views.py stays the single owner of ABP/Outlook logic).
    """
    dates, y = load_series()
    today = today or date.today()

    # Today is still being worked, so it is not history. Treating it as history
    # is wrong twice over: its part-day total drags the trailing rate down, and
    # it vanishes from "days left" — on the 28th of a 31-day month the crew has
    # 4 days to work, not 3. History therefore stops at yesterday and today is
    # the first simulated day; the same rule gives the FY count 28 Aug -> 31 Mar
    # inclusive, matching the header countdown.
    partial_today = float(sum(v for d, v in zip(dates, y) if d == today))
    n_hist = bisect.bisect_right(dates, today - timedelta(days=1))
    dates, y = dates[:n_hist], y[:n_hist]

    if len(y) < LEVEL_WINDOW + BLOCK * 2:
        return {'available': False,
                'reason': 'Not enough daily history — run seed_eq_wise.py.'}

    start, end = period_bounds(scope, today)
    months = np.array([d.month for d in dates])
    midx = month_index(dates, y)

    hist = [(d, v) for d, v in zip(dates, y) if d >= start]
    achieved = float(sum(v for _, v in hist))
    last_actual = dates[-1]

    future = []
    d = max(last_actual + timedelta(days=1), start)
    while d <= end:
        future.append(d)
        d += timedelta(days=1)

    deseason = y / midx[months]
    level = float(np.median(deseason[-LEVEL_WINDOW:]))
    levels = _levels(deseason)
    ratios = _ratios(deseason, levels)
    sigma = _level_sigma(levels)

    rng = np.random.default_rng(seed)
    sims = _simulate(level, midx, np.array([f.month for f in future], dtype=int),
                     ratios, sigma, rng)
    paths = achieved + np.cumsum(sims, axis=1) if len(future) else np.zeros((N_SIMS, 0))
    totals = paths[:, -1] if len(future) else np.full(N_SIMS, achieved)

    p_hit = None
    if target:
        p_hit = float((totals >= float(target)).mean())

    # Actual cumulative to date, for the "actual vs forecast" line.
    run, actual = 0.0, []
    for d, v in hist:
        run += v
        actual.append({'d': d.isoformat(), 'v': round(run, 1)})

    def band(q):
        return [{'d': f.isoformat(), 'v': round(float(v), 1)}
                for f, v in zip(future, np.percentile(paths, q, axis=0))]

    def dband(q):
        """Same percentiles on the per-day rate, not the cumulative total."""
        return [{'d': f.isoformat(), 'v': round(float(v), 1)}
                for f, v in zip(future, np.percentile(sims, q, axis=0))]

    return {
        'available':    True,
        'scope':        scope,
        'as_of':        last_actual.isoformat(),
        'period':       {'start': start.isoformat(), 'end': end.isoformat()},
        'target':       float(target) if target else None,
        'achieved':     round(achieved, 1),
        # Booked so far on the in-progress day, reported separately so the drop
        # in `achieved` against the dashboard's month-to-date card is explained.
        'partial_today': round(partial_today, 1),
        'days_left':    len(future),   # includes today, which is still to be worked
        'p_hit':        p_hit,
        # Plain average of the last 60 actual days — directly comparable to
        # required_daily. `level` is the de-seasonalised version the model runs
        # on, which reads oddly next to it (in monsoon it sits much higher).
        'daily_rate':   round(float(np.mean(y[-LEVEL_WINDOW:])), 1),
        'forecast_daily': (round(float(np.median(sims.sum(axis=1))) / len(future), 1)
                           if len(future) else None),
        'required_daily': (round((float(target) - achieved) / len(future), 1)
                           if target and future else None),
        # P90 = optimistic (beaten only 10% of the time), P10 = pessimistic.
        'optimistic':   round(float(np.percentile(totals, 90)), 1),
        'expected':     round(float(np.percentile(totals, 50)), 1),
        'pessimistic':  round(float(np.percentile(totals, 10)), 1),
        'month_index':  [round(float(v), 3) for v in midx[1:]],
        'actual':       actual,
        'p10':          band(10),
        'p50':          band(50),
        'p90':          band(90),
        'daily_actual': [{'d': d.isoformat(), 'v': round(v, 1)} for d, v in hist],
        'daily_p10':    dband(10),
        'daily_p50':    dband(50),
        'daily_p90':    dband(90),
    }
