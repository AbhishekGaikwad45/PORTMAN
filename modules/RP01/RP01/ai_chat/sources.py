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
    'mbc-ops':          'Barge/MBC cargo movements, one row per document.',
    'vessel-ops':       'Mother vessel voyages, one row per vessel.',
    'vessel-barge':     'Vessel-to-barge transfers: which barge took what from which hold.',
    'vessel-anchorage': 'Vessel waiting and anchorage events.',
    'lueu-equipment':   'Equipment and shift utilisation, current records.',
    'lueu-historical':  'Equipment utilisation including the pre-cutover archive. Use only for dates older than the current records.',
    'mbc-tat':          'Barge/MBC turnaround, one row per document with each leg timed.',
    PORT_OVERVIEW:      'Live dashboard as of right now: throughput against target, berth occupancy, top delays, MBC status, weather. No history.',
}

# What each source actually contains. MEASURES are the only columns that may be
# summed or averaged; everything in LABELS is text to group by. Splitting them
# here is what stops the model trying to total a column like Delay, which is a
# delay *name*, not a duration.
#
# These are the starting point, not the last word: _seen_columns below replaces
# them with the real keys the moment a source is actually queried, so a schema
# change in custom_report corrects itself rather than silently misleading.
MEASURES = {
    'mbc-ops':          ['BL Qty'],
    'vessel-barge':     ['Discharge Qty', 'Initial Draft Survey Qty', 'Trip No'],
    'lueu-equipment':   ['Quantity', 'Diff Hrs'],
    'lueu-historical':  ['Quantity', 'Diff Hrs'],
    'mbc-tat':          ['TAT (min)', 'Loading Time (min)', 'Unloading Time (min)',
                         'Preberthing (min)', 'Total at Jaigad (min)',
                         'Total at Dharamtar (min)', 'Gull Waiting (min)',
                         'Wait After Load (min)', 'Wait After Unload (min)',
                         'BL Quantity'],
}

LABELS = {
    'mbc-ops':          ['Cargo Type', 'Cargo Name', 'Cargo Category', 'Customer',
                         'MBC Name', 'Operation Type', 'DP Unloading Berth', 'Status',
                         'Doc No', 'Doc Date', 'Year', 'Year-Month'],
    'vessel-barge':     ['Vessel', 'Barge', 'Cargo', 'Cargo Type', 'Hold', 'Contractor',
                         'Port Crane', 'Crane Loaded From', 'BPT/BFL', 'VCN No',
                         'Status', 'NOR Date', 'Year', 'Year-Month'],
    'lueu-equipment':   ['Equipment', 'Operator', 'Shift', 'Shift Incharge', 'Berth',
                         'Cargo', 'Cargo Type', 'Delay', 'Delay Type', 'System',
                         'Route', 'Barge / MBC Name', 'UOM', 'Date', 'Year', 'Year-Month'],
    'lueu-historical':  ['Equipment', 'Operator', 'Shift', 'Shift Incharge', 'Berth',
                         'Cargo', 'Cargo Type', 'Delay', 'Delay Type', 'System',
                         'Route', 'Barge / MBC Name', 'UOM', 'Date', 'Year', 'Year-Month'],
    'mbc-tat':          ['MBC Name', 'Cargo', 'Cargo Type', 'Operation Type', 'Status',
                         'Doc No', 'Doc Date', 'Year', 'Year-Month'],
}

# Real column names seen on the last successful fetch, per source.
_seen_columns = {}


def remember_columns(source, columns):
    """Record the columns a real query returned, so the catalog self-corrects."""
    if columns:
        _seen_columns[source] = list(columns)


def columns_for(source):
    """(measures, labels) for a source, preferring what we have actually seen."""
    measures = list(MEASURES.get(source, []))
    labels = list(LABELS.get(source, []))
    seen = _seen_columns.get(source)
    if seen:
        # Keep the curated measure/label split, but drop anything that no longer
        # exists and append columns we did not know about.
        measures = [c for c in measures if c in seen]
        labels = [c for c in labels if c in seen]
        known = set(measures) | set(labels)
        labels += [c for c in seen if c not in known and not c.startswith('_')]
    return measures, labels


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
    """One line per source: what it is, what can be totalled, what can be
    grouped. The measure/label split is the part that stops the model routing a
    tonnage question to a source with no tonnage column."""
    lines = []
    for key in ALL_SOURCES:
        measures, labels = columns_for(key)
        parts = ['- %s: %s' % (key, DESCRIPTIONS[key])]
        if measures:
            parts.append('  Numeric (can total/average): %s' % ', '.join(measures))
        if labels:
            parts.append('  Text (group by only): %s' % ', '.join(labels[:14]))
        cols = valid_date_cols(key)
        if cols:
            parts.append('  date_col: %s' % ', '.join(cols))
        lines.append('\n'.join(parts))
    return '\n'.join(lines)
