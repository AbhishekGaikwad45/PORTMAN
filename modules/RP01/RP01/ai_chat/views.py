"""RP01 AI chat — natural-language questions about port operations.

Two kinds of data reach the model:

  FIXED    the live Port Overview dashboard and the full FY target table.
           Always in context, never queried, refreshed on a short TTL.
  QUERIED  equipment and shift utilisation records (lueu-equipment), fetched
           through the existing Custom Report query and copied into a
           throwaway in-memory SQLite table the model may write SELECTs over.

Because there is exactly one queryable source, stage 1 no longer routes
between sources: it only decides whether the question is in scope, which
period to cover, and whether the records need querying at all — a question
answerable from the dashboard skips the SQL stage entirely.
"""

import json
import re
import sqlite3
import time
from datetime import date
from functools import wraps
from urllib.parse import quote, urlparse, urlunparse

import psycopg2
from flask import jsonify, redirect, render_template, request, session, url_for

from config import DATABASE_URL

from .. import bp
from ..custom_report.views import fetch_source_rows
from . import ollama, sandbox, sources

MAX_NARRATE_ROWS = 100
MAX_DISPLAY_ROWS = 500
MAX_RESULT_CHARS = 2500
MAX_FIXED_CHARS = 2600
CONTEXT_TTL = 60          # seconds; the dashboard is "live", not per-millisecond
CHART_TYPES = ['bar', 'line', 'pie', 'doughnut', 'scatter', 'none']

_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Not logged in'}), 401
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated


TRIAGE_SCHEMA = {
    'type': 'object',
    'properties': {
        'in_scope':    {'type': 'boolean'},
        'needs_query': {'type': 'boolean'},
        'period':      {'type': 'string', 'enum': sources.PERIODS},
        'from_date':   {'type': 'string'},
        'to_date':     {'type': 'string'},
    },
    'required': ['in_scope', 'needs_query', 'period'],
}

SQL_SCHEMA = {
    'type': 'object',
    'properties': {
        'sql': {'type': 'string'},
        'chart': {
            'type': 'object',
            'properties': {
                'type':  {'type': 'string', 'enum': CHART_TYPES},
                'x':     {'type': 'string'},
                'y':     {'type': 'array', 'items': {'type': 'string'}},
                'title': {'type': 'string'},
            },
            'required': ['type', 'x', 'y', 'title'],
        },
    },
    'required': ['sql', 'chart'],
}


class OutOfScope(Exception):
    """The question is not about this port's operations."""


def readonly_connection(cfg):
    """Open the source query on a read-only role, when one is configured.

    Belt and braces on top of the sqlite sandbox: the sandbox stops the model's
    own SQL touching Postgres, and this stops the whole AI path writing
    anything even if some other guard fails. Returns None to fall back to the
    app's normal connection.
    """
    user = (cfg.get('db_user') or '').strip()
    if not user:
        return None
    pw = cfg.get('db_password') or ''
    p = urlparse(DATABASE_URL)
    netloc = '%s:%s@%s' % (quote(user, safe=''), quote(pw, safe=''), p.hostname)
    if p.port:
        netloc += ':%d' % p.port
    conn = psycopg2.connect(urlunparse(p._replace(netloc=netloc)))
    conn.cursor().execute('SET default_transaction_read_only = on')
    return conn


# -- history -----------------------------------------------------------------

def trim_history(history, max_chars):
    """Keep the whole conversation until it exceeds the budget, then drop the
    oldest turns. The context window is a hard limit, not a preference, and on
    CPU a long thread costs ingestion time on every single turn."""
    msgs, total, trimmed = [], 0, False
    for turn in reversed(history or []):
        role = 'assistant' if turn.get('role') == 'assistant' else 'user'
        content = str(turn.get('content', ''))[:2000]
        if total + len(content) > max_chars:
            trimmed = True
            break
        msgs.append({'role': role, 'content': content})
        total += len(content)
    return list(reversed(msgs)), trimmed


# -- fixed context: port overview + FY targets -------------------------------

def slim_overview(payload):
    """Drop what a chatbot cannot use - mainly the berth box pixel geometry."""
    berths = [{'berth': b.get('label'), 'assets': b.get('assets')}
              for b in (payload.get('layout') or {}).get('berths', [])]
    return {
        'as_of':          payload.get('as_of'),
        'cargo_cards':    payload.get('cargo_cards'),
        'berths':         berths,
        'upcoming':       payload.get('upcoming'),
        'delays':         payload.get('delays'),
        'mbc_status':     payload.get('mbc_status'),
        'shift_incharge': payload.get('shift_incharge'),
        'notes':          payload.get('notes'),
        'weather':        payload.get('weather'),
    }


def _fetch_overview():
    # ponytail: reuses the dashboard view's own Response rather than splitting
    # another 100-line builder out of it. Only valid inside a request context.
    from ..port_overview.views import port_overview_data
    return slim_overview(port_overview_data().get_json())


def _fetch_targets():
    from ..daily_ops.model import fy_label
    from ..port_overview.views import _load_fy_targets, _fy_target_totals
    today = date.today()
    fy = fy_label(today.year if today.month >= 4 else today.year - 1)
    base, effective = _fy_target_totals(fy)
    months = [{'month': t.get('month'),
               'base': t.get('base_target'),
               'outlook': t.get('outlook')}
              for t in _load_fy_targets(fy)]
    return {'financial_year': fy, 'fy_base_target': base,
            'fy_effective_target': effective, 'monthly': months}


_cache = {'at': 0.0, 'data': None}


def fixed_context(ttl=CONTEXT_TTL):
    """Dashboard + targets, cached briefly.

    Every question carries this, so without a TTL the dashboard's dozen-odd
    queries would run on every keystroke-to-answer round trip for data that
    changes on the order of minutes.
    """
    now = time.time()
    if _cache['data'] is not None and now - _cache['at'] < ttl:
        return _cache['data']
    data = {'overview': _fetch_overview(), 'targets': _fetch_targets()}
    _cache.update(at=now, data=data)
    return data


def _card_line(card):
    if not isinstance(card, dict):
        return ''
    bits = ['%s MT' % _fmt(card.get('total') or 0)]
    if card.get('target'):
        bits.append('target %s' % _fmt(card['target']))
    by_type = card.get('by_type') or {}
    if by_type:
        bits.append('by type: ' + ', '.join(
            '%s %s' % (k, _fmt(v)) for k, v in by_type.items()))
    by_shift = card.get('by_shift') or {}
    if by_shift:
        bits.append('by shift: ' + ', '.join(
            '%s %s' % (k, _fmt(v)) for k, v in by_shift.items()))
    return ' | '.join(bits)


def render_fixed_context(ctx):
    """Compact prose beats raw JSON here - a 7B reads lines far better than it
    reads nested objects, and it costs a fraction of the tokens."""
    ov, tg = ctx.get('overview') or {}, ctx.get('targets') or {}
    lines = ['LIVE PORT DASHBOARD (as of %s) - available without querying:'
             % ov.get('as_of', 'unknown')]

    for key in ('today', 'yesterday', 'current_month', 'current_fy', 'all_time'):
        card = (ov.get('cargo_cards') or {}).get(key)
        if card:
            lines.append('  %s: %s' % (card.get('label', key), _card_line(card)))

    if tg:
        lines.append('  FY %s targets: base %s MT, effective %s MT'
                     % (tg.get('financial_year'), _fmt(tg.get('fy_base_target') or 0),
                        _fmt(tg.get('fy_effective_target') or 0)))
        months = [m for m in (tg.get('monthly') or []) if m.get('base')]
        if months:
            lines.append('  Monthly targets: ' + ', '.join(
                '%s %s%s' % (m['month'][:3], _fmt(m['base']),
                             '/outlook %s' % _fmt(m['outlook']) if m.get('outlook') else '')
                for m in months))

    occupied = [b for b in (ov.get('berths') or []) if b.get('assets')]
    if occupied:
        lines.append('  Berths in use: ' + '; '.join(
            '%s %s' % (b['berth'], json.dumps(b['assets'], default=str)[:110])
            for b in occupied[:12]))

    for key, label in (('delays', 'Top delays today'), ('mbc_status', 'MBC status'),
                       ('upcoming', 'Upcoming arrivals'), ('weather', 'Weather')):
        val = ov.get(key)
        if val:
            lines.append('  %s: %s' % (label, json.dumps(val, default=str)[:420]))

    if ov.get('shift_incharge'):
        lines.append('  Shift incharge: %s' % ov['shift_incharge'])
    if ov.get('notes'):
        lines.append('  Notes: %s' % json.dumps(ov['notes'], default=str)[:300])

    return '\n'.join(lines)[:MAX_FIXED_CHARS]


# -- stage 1: triage ---------------------------------------------------------

def triage(question, history_msgs, cfg, want_period=None):
    """Decide scope, period, and whether the records need querying.

    There is only one queryable source, so nothing routes between sources any
    more. When the user has already chosen a period the model still decides
    whether a query is needed, but its period choice is pinned to theirs.
    """
    measures, labels = sources.columns_for(sources.ONLY_SOURCE)
    schema = dict(TRIAGE_SCHEMA, properties=dict(TRIAGE_SCHEMA['properties']))
    if want_period:
        schema['properties']['period'] = {'type': 'string', 'enum': [want_period]}

    system = (
        'You triage questions about this port\'s operations.\n'
        'Today is ' + date.today().isoformat() + '. The financial year runs 1 April '
        'to 31 March.\n\n'
        'Always available without querying: the live dashboard (today, yesterday, '
        'this month and this financial year throughput against target, berth '
        'occupancy, top delays, MBC status, upcoming arrivals, weather) and the '
        'full financial-year target table.\n\n'
        'Queryable records - equipment and shift utilisation:\n'
        '  Numeric (can total/average): ' + ', '.join(measures) + '\n'
        '  Text (group by only): ' + ', '.join(labels) + '\n\n'
        'Decide:\n'
        '- in_scope: false ONLY when the question has nothing to do with this port '
        '- general knowledge, chit-chat, coding, other companies. Anything about '
        'cargo, barges, vessels, equipment, shifts, berths, delays, operators, '
        'throughput or targets is in scope.\n'
        '- needs_query: true when answering needs the equipment/shift records above '
        '(anything about equipment, operators, shifts, berths, delays, or a period '
        'the dashboard does not cover). false when the live dashboard and targets '
        'already answer it.\n'
        '- period: which date range the records should cover. Use custom only when '
        'the question names explicit dates, and then also set from_date and to_date '
        'as YYYY-MM-DD. If no period is stated, use this_month.'
    )
    msgs = [{'role': 'system', 'content': system}] + history_msgs + \
           [{'role': 'user', 'content': question}]
    out = json.loads(ollama.chat(msgs, cfg=cfg, schema=schema, num_predict=100))

    if out.get('in_scope') is False:
        raise OutOfScope()
    return settle(out.get('period'), out, want_period=want_period,
                  needs_query=out.get('needs_query') is not False)


def settle(period, out, want_period=None, needs_query=True):
    """Validate a triage decision and turn its named period into real dates."""
    period = want_period or period or 'this_month'
    if period == 'custom':
        frm, to = out.get('from_date', ''), out.get('to_date', '')
        if not _ISO.match(frm or '') or not _ISO.match(to or ''):
            raise ValueError('Model asked for a custom range but gave malformed dates')
        if frm > to:
            frm, to = to, frm
    else:
        if period not in sources.PERIODS:
            period = 'this_month'
        frm, to = sources.resolve_period(period)
    return {'source': sources.ONLY_SOURCE,
            'date_col': sources.default_date_col(sources.ONLY_SOURCE),
            'period': period, 'from_date': frm, 'to_date': to,
            'needs_query': bool(needs_query)}


# -- stage 2: sql ------------------------------------------------------------

def _sql_system(ddl, rows):
    # Kept deliberately tight: on CPU this stage is dominated by prompt
    # ingestion, so every line of schema and every sample row costs seconds.
    samples = json.dumps(rows[:2], default=str)[:600]
    return (
        'Write one SQLite SELECT over a table named "data".\n\n'
        + ddl + '\n\nExample rows: ' + samples + '\n\n'
        'Rules:\n'
        '- Double-quote every column name; most contain spaces.\n'
        '- Use only columns listed above. Never invent one.\n'
        '- Only SUM or AVG columns typed INTEGER or REAL. TEXT columns are labels: '
        'GROUP BY them or COUNT them, never total them.\n'
        '- "which/who/top/most/least" means GROUP BY the label column and ORDER BY '
        'the measure DESC.\n'
        '- The table is already filtered to the date range. Do not filter on dates '
        'again unless the question asks for a breakdown over time.\n'
        '- Alias every aggregate, e.g. SUM("Quantity") AS "Total Quantity".\n\n'
        'Also pick a chart. Use "bar" for a breakdown by label, "line" over time, and '
        '"none" only when the answer is a single number.\n'
        'chart.x is the label column your SELECT outputs, chart.y the numeric ones. '
        'Write both as plain names with NO quotes: Equipment, not "Equipment".'
    )


def _match_column(name, cols):
    """Resolve a model-supplied column name to a real result column.

    The SQL rules tell the model to double-quote every column, and it carries
    those quotes into the chart spec too - it answers with '"Equipment"' where
    the result column is Equipment. Strip the quoting it was told to add and
    fall back to a case-insensitive match rather than throwing the chart away
    over punctuation.
    """
    if not isinstance(name, str):
        return None
    bare = name.strip().strip('"').strip("'").strip()
    if bare in cols:
        return bare
    lowered = {c.lower(): c for c in cols}
    return lowered.get(bare.lower())


def validate_chart(chart, cols):
    """A hallucinated column name must fail visibly, not render an empty chart."""
    if not isinstance(chart, dict) or chart.get('type') in (None, 'none'):
        return None, None
    x = _match_column(chart.get('x'), cols)
    if not x:
        return None, 'chart x-axis %r is not a column in the result' % (chart.get('x'),)
    ys, seen = [], set()
    for y in chart.get('y') or []:
        m = _match_column(y, cols)
        if m and m != x and m not in seen:
            seen.add(m)
            ys.append(m)
    if not ys:
        return None, 'chart has no numeric y column in the result'
    return {'type': chart['type'], 'x': x, 'y': ys,
            'title': chart.get('title', '')}, None


def generate_and_run(question, history_msgs, conn, ddl, rows, cfg):
    """LLM writes SQL; on a database error, hand the error back once and retry.

    The single self-correction pass is the cheapest accuracy available here —
    it turns most hallucinated column names into a working second attempt.
    """
    model = cfg.get('sql_model') or cfg['model']
    msgs = [{'role': 'system', 'content': _sql_system(ddl, rows)}] + history_msgs + \
           [{'role': 'user', 'content': question}]

    last_err = None
    for attempt in (1, 2):
        out = json.loads(ollama.chat(msgs, cfg=cfg, schema=SQL_SCHEMA, model=model,
                                     num_predict=300))
        sql = out.get('sql', '')
        try:
            cols, result = sandbox.run(conn, sql, limit=cfg['max_result_rows'])
        except (sqlite3.Error, ValueError) as e:
            last_err = '%s: %s' % (type(e).__name__, e)
            msgs = msgs + [
                {'role': 'assistant', 'content': sql},
                {'role': 'user', 'content':
                    'That query failed with: ' + last_err +
                    '\nRewrite it using only columns from the schema.'},
            ]
            continue
        chart, chart_error = validate_chart(out.get('chart'), cols)
        return {'sql': sql, 'columns': cols, 'rows': result, 'chart': chart,
                'chart_error': chart_error, 'retried': attempt == 2}
    raise ValueError('SQL failed after one retry - ' + str(last_err))


# -- stage 3: narrate --------------------------------------------------------

def _as_table(cols, rows):
    out = [' | '.join(cols)]
    for r in rows[:MAX_NARRATE_ROWS]:
        out.append(' | '.join('' if v is None else str(v) for v in r))
    return '\n'.join(out)[:MAX_RESULT_CHARS]


def _fmt(v):
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return '{:,}'.format(v) if isinstance(v, (int, float)) else str(v)


def ensure_answer(answer, columns, rows):
    """Guarantee a single-value result actually appears in the prose.

    Small models narrate around a lone number - "the total has been
    calculated" with no total in sight. When the result is one cell, state the
    value outright rather than trusting the model to.
    """
    answer = (answer or '').strip()
    if len(rows) != 1 or len(columns) != 1:
        return answer or 'No readable answer came back. Check the SQL below.'

    value = rows[0][0]
    if value is None:
        return answer or ('%s came back empty for that period.' % columns[0])

    shown = _fmt(value)
    if answer and (shown in answer or str(value) in answer):
        return answer
    stated = '%s: %s' % (columns[0], shown)
    return stated + ('. ' + answer if answer else '.')


def narrate(question, history_msgs, context, cfg):
    system = (
        'You are an analyst for this port. Answer using ONLY the data below.\n\n'
        'How to answer:\n'
        '- Lead with the number: "IBRM discharge was 42,150 MT." Never open with '
        '"Based on the data provided" or "According to".\n'
        '- One to three sentences. No preamble, no restating the question, no '
        'offering further help at the end.\n'
        '- Quantities are metric tonnes (MT) unless the column name says otherwise. '
        'Durations in minutes over 120 should be given in hours.\n'
        '- Format thousands with commas.\n'
        '- Add one comparison only when the data shows it: the largest contributor, '
        'progress against target, or the direction of travel.\n'
        '- Never reproduce a table or list it row by row. The reader already has it '
        'on screen. Summarise: the total, the top two or three, anything odd.\n'
        '- If a result table is present and has rows, answer from it. Only say there '
        'is no data when it is genuinely empty, and then say what was searched and '
        'what to try instead. Do not apologise.\n'
        '- Never state a number that is not in the data, and never estimate.\n\n'
        + context
    )
    msgs = [{'role': 'system', 'content': system}] + history_msgs + \
           [{'role': 'user', 'content': question}]
    return ollama.chat(msgs, cfg=cfg, num_predict=350).strip()


# -- endpoints ---------------------------------------------------------------

@bp.route('/module/RP01/ai-chat/')
@admin_required
def ai_chat_index():
    """Internal test page. Deliberately not linked from rp01.html, and admin
    gated so being unlinked is not the only thing keeping users out."""
    return render_template('ai_chat/ai_chat.html', username=session.get('username'))


@bp.route('/api/module/RP01/ai-chat/sources')
@login_required
def ai_chat_sources():
    measures, labels = sources.columns_for(sources.ONLY_SOURCE)
    return jsonify({
        'source': sources.ONLY_SOURCE,
        'description': sources.DESCRIPTIONS[sources.ONLY_SOURCE],
        'measures': measures,
        'labels': labels,
        'periods': sources.PERIODS,
        'fixed_context': ['Live port dashboard (throughput vs target, berths, '
                          'delays, MBC status, weather)',
                          'Financial-year targets, monthly and total'],
    })


@bp.route('/api/module/RP01/ai-chat/ask', methods=['POST'])
@login_required
def ai_chat_ask():
    cfg = ollama.get_config()
    if not cfg.get('enabled'):
        return jsonify({'error': 'AI chat is disabled. Enable it in Admin > AI Config.'}), 503

    body = request.get_json(silent=True) or {}
    question = (body.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'question is required'}), 400

    want_period = (body.get('period') or '').strip() or None
    if want_period and want_period not in sources.PERIODS:
        return jsonify({'error': 'Unknown period: %s' % want_period}), 400

    history, history_trimmed = trim_history(body.get('history'), cfg['max_history_chars'])
    timing = {}
    source_rows = None
    row_count = None
    result = {'chart': None, 'chart_error': None, 'sql': None,
              'columns': [], 'rows': [], 'retried': False}

    try:
        t = time.time()
        fixed = render_fixed_context(fixed_context())
        timing['context'] = round(time.time() - t, 2)

        t = time.time()
        picked = triage(question, history, cfg, want_period)
        timing['triage'] = round(time.time() - t, 2)

        parts = [fixed]

        if picked['needs_query']:
            t = time.time()
            ro = readonly_connection(cfg)
            try:
                rows = fetch_source_rows(picked['source'], picked['date_col'],
                                         picked['from_date'], picked['to_date'],
                                         conn=ro)
            finally:
                if ro:
                    ro.close()
            timing['fetch'] = round(time.time() - t, 2)
            source_rows = len(rows)
            if rows:
                sources.remember_columns(picked['source'], list(rows[0].keys()))

            if len(rows) > cfg['max_rows']:
                return jsonify(dict(
                    picked, timing=timing,
                    error='That range returns %s rows (limit %s). Ask again with a '
                          'narrower date range.' % (format(len(rows), ','),
                                                    format(cfg['max_rows'], ',')))), 413

            if not rows:
                parts.append('EQUIPMENT RECORDS: none between %s and %s.'
                             % (picked['from_date'], picked['to_date']))
            else:
                conn, ddl = sandbox.load(rows)
                try:
                    t = time.time()
                    result = generate_and_run(question, history, conn, ddl, rows, cfg)
                    timing['sql'] = round(time.time() - t, 2)
                finally:
                    conn.close()
                row_count = len(result['rows'])

                if row_count == 1 and len(result['columns']) == 1:
                    parts.append('EQUIPMENT RECORDS (%s to %s) returned a single value: '
                                 '%s = %s. Say that number.'
                                 % (picked['from_date'], picked['to_date'],
                                    result['columns'][0], _fmt(result['rows'][0][0])))
                else:
                    parts.append('EQUIPMENT RECORDS (%s to %s), %d rows:\n%s'
                                 % (picked['from_date'], picked['to_date'], row_count,
                                    _as_table(result['columns'], result['rows'])))

        t = time.time()
        answer = ensure_answer(narrate(question, history, '\n\n'.join(parts), cfg),
                               result['columns'], result['rows'])
        timing['narrate'] = round(time.time() - t, 2)

    except OutOfScope:
        return jsonify({
            'answer': ('I only answer questions about this port. Ask about cargo '
                       'throughput, targets, berths, equipment, operators, shifts '
                       'or delays.'),
            'source': None, 'date_col': '', 'period': '', 'from_date': '', 'to_date': '',
            'needs_query': False, 'sql': None, 'sql_retried': False,
            'columns': [], 'rows': [], 'row_count': None, 'chart': None,
            'chart_error': None, 'source_rows': None, 'out_of_scope': True,
            'history_trimmed': history_trimmed, 'timing': timing,
        })
    except Exception as e:
        return jsonify({'error': '%s: %s' % (type(e).__name__, e), 'timing': timing}), 502

    shown = result['rows'][:MAX_DISPLAY_ROWS]
    return jsonify(dict(
        picked,
        answer=answer,
        rows_truncated=len(result['rows']) > len(shown),
        sql=result['sql'],
        sql_retried=result['retried'],
        columns=result['columns'],
        rows=shown,
        row_count=row_count,
        chart=result['chart'],
        chart_error=result['chart_error'],
        source_rows=source_rows,
        history_trimmed=history_trimmed,
        timing=timing,
    ))
