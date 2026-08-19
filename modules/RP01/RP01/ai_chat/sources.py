"""What the AI chat is allowed to look at, and how it picks.

Descriptions name the *key columns* on purpose. A 3B model cannot know that
"IBRM" is a cargo type living in mbc-ops and absent from vessel-ops unless the
column list tells it, and routing to a source that lacks the column is the
single most common failure. Keep each entry to one line — small models start
ignoring options once the choice list gets wordy.

Full column lists are NOT here: those are read off the live query result at
request time, so stage 3 can never drift from custom_report.
"""

from datetime import date, timedelta

from ..custom_report.views import DATE_COL_FILTERS, DATE_COL_DEFAULTS, VALID_SOURCES

PORT_OVERVIEW = 'port-overview'

# Cargo categories used across RP01 (mirrors port_overview.TARGET_CATEGORIES).
# Named in the prompt so the router recognises them as cargo-type values
# rather than vessel or berth names.
CARGO_TYPES = ['IBRM', 'CBRM', 'FLUXES', 'CLINKER', 'SLAG']

DESCRIPTIONS = {
    'mbc-ops':          'Barge/MBC cargo movements. Key columns: Cargo Type, Cargo Name, BL Qty, Customer, MBC Name, Operation Type, DP Unloading Berth, Doc Date.',
    'vessel-ops':       'Mother vessel voyages, one row per vessel. Key columns: Vessel, VCN No, Vessel Agent, Cargo, BL Qty (MT), Actual Days, NOR Date. No cargo-type breakdown.',
    'vessel-barge':     'Vessel-to-barge transfers. Key columns: Vessel, Barge, Trip No, Hold, Discharge Qty, Contractor, Port Crane, Cargo Type.',
    'vessel-anchorage': 'Vessel waiting and anchorage events. Key columns: Vessel, Anchorage, Anchored, Anchor Aweigh, Cargo Qty, Cargo Type.',
    'lueu-equipment':   'Equipment and shift utilisation, current records. Key columns: Equipment, Operator, Shift, Berth, Quantity, Delay, Delay Type, Cargo Type, Date.',
    'lueu-historical':  'Same as lueu-equipment but includes the pre-cutover archive. Use only for dates older than the current records.',
    'mbc-tat':          'Barge/MBC turnaround times in minutes. Key columns: TAT (min), Loading Time (min), Unloading Time (min), Preberthing (min), MBC Name, Cargo Type.',
    PORT_OVERVIEW:      'Live dashboard as of right now: today/month/FY throughput against target, berth occupancy, top delays, MBC status, weather. No history.',
}

# Explicit tie-breakers for the questions people actually ask. Cheaper and far
# more reliable than hoping the model infers them from the descriptions.
ROUTING_HINTS = """Choosing between sources:
- Cargo tonnage, throughput or discharge broken down by cargo type -> mbc-ops
- Anything naming """ + ', '.join(CARGO_TYPES) + """ (these are Cargo Type values) -> mbc-ops
- A named vessel, its holds, cranes or the barges it fed -> vessel-barge
- Equipment, operators, shifts, berths or delays -> lueu-equipment
- How long barges took -> mbc-tat
- "right now", "current", "today so far", against-target -> port-overview"""

# Only these are trusted to answer questions. vessel-ops and vessel-anchorage
# are excluded by request - vessel-ops has no Cargo Type column at all, so it
# can only mislead on the cargo questions people actually ask.
# To re-enable one, add its key back here.
TRUSTED = ['mbc-ops', 'mbc-tat', 'vessel-barge', 'lueu-equipment',
           'lueu-historical', PORT_OVERVIEW]

ALL_SOURCES = [k for k in TRUSTED if k in VALID_SOURCES or k == PORT_OVERVIEW]

# The model picks a named period; Python does the date arithmetic. Small models
# are unreliable at working out "yesterday" or a financial-year boundary, and
# an off-by-one there silently returns the wrong answer.
PERIODS = ['today', 'yesterday', 'last_7_days', 'this_month', 'last_month',
           'this_fy', 'last_fy', 'all_time', 'custom']


def fy_start(d):
    """Financial year runs 1 April to 31 March."""
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def resolve_period(period, today=None):
    """Named period -> (from_date, to_date) as ISO strings."""
    d = today or date.today()
    if period == 'today':
        start = end = d
    elif period == 'yesterday':
        start = end = d - timedelta(days=1)
    elif period == 'last_7_days':
        start, end = d - timedelta(days=6), d
    elif period == 'this_month':
        start, end = d.replace(day=1), d
    elif period == 'last_month':
        end = d.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif period == 'this_fy':
        start, end = fy_start(d), d
    elif period == 'last_fy':
        this = fy_start(d)
        start = date(this.year - 1, 4, 1)
        end = this - timedelta(days=1)
    elif period == 'all_time':
        start, end = date(1900, 1, 1), d
    else:
        raise ValueError('Unknown period: %r' % (period,))
    return start.isoformat(), end.isoformat()


def is_valid(source):
    return source in ALL_SOURCES


def valid_date_cols(source):
    return sorted(DATE_COL_FILTERS.get(source, {}).keys())


def default_date_col(source):
    return DATE_COL_DEFAULTS.get(source, '')


def catalog_prompt():
    """One line per source, with its allowed date columns. Fed to stage 1."""
    lines = []
    for key in ALL_SOURCES:
        cols = valid_date_cols(key)
        suffix = '  [date_col: %s]' % ', '.join(cols) if cols else '  [date_col: none]'
        lines.append('- %s: %s%s' % (key, DESCRIPTIONS[key], suffix))
    return '\n'.join(lines)
