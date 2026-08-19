"""What the AI chat is allowed to look at.

Only the human-written descriptions live here. Column names deliberately
do NOT — they are read off the actual query result at request time
(``rows[0].keys()``), so this file can never drift out of sync with
custom_report. Descriptions are one line each on purpose: small models
start ignoring options once the choice list gets wordy.
"""

from ..custom_report.views import DATE_COL_FILTERS, DATE_COL_DEFAULTS, VALID_SOURCES

# The live Port Overview dashboard is not a pivot source — it is already
# aggregated, so it skips the SQL stage entirely (see views._answer_overview).
PORT_OVERVIEW = 'port-overview'

DESCRIPTIONS = {
    'mbc-ops':          'Barge/MBC loading and unloading operations: BL quantity, cargo type, customer, berths, load/discharge timings.',
    'vessel-ops':       'Mother vessel discharge operations: vessel, agent, cargo, BL quantity in MT, days alongside, NOR date.',
    'vessel-barge':     'Vessel-to-barge transfers: which barge took what quantity from which vessel hold, crane used, contractor.',
    'vessel-anchorage': 'Vessel anchorage events: anchored, discharge started/commenced, anchor aweigh, cargo quantity.',
    'lueu-equipment':   'Equipment utilisation log (current): equipment, operator, shift, berth, quantity handled, delays and delay types.',
    'lueu-historical':  'Equipment utilisation log including the pre-cutover historical archive — use for anything older than the current records.',
    'mbc-tat':          'Barge/MBC turnaround time broken into legs in minutes: preberthing, loading, transit, waiting, unloading, total TAT.',
    PORT_OVERVIEW:      'Live port dashboard right now: today/yesterday/month/FY throughput vs target, berth occupancy, top delays, MBC status, weather.',
}

ALL_SOURCES = sorted(VALID_SOURCES) + [PORT_OVERVIEW]


def is_valid(source):
    return source in VALID_SOURCES or source == PORT_OVERVIEW


def valid_date_cols(source):
    return sorted(DATE_COL_FILTERS.get(source, {}).keys())


def default_date_col(source):
    return DATE_COL_DEFAULTS.get(source, '')


def catalog_prompt():
    """One line per source, with its allowed date columns. Fed to stage 1."""
    lines = []
    for key in ALL_SOURCES:
        cols = valid_date_cols(key)
        suffix = f"  [date_col: {', '.join(cols)}]" if cols else '  [date_col: none]'
        lines.append(f"- {key}: {DESCRIPTIONS[key]}{suffix}")
    return '\n'.join(lines)
