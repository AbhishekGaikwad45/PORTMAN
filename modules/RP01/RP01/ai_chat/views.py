"""RP01 AI chat — natural-language questions over the Custom Report sources.

Flow for a pivot source:
  1. route    (LLM) pick source + date column + date range
  2. fetch          run the existing trusted custom_report query
  3. sql      (LLM) write one SELECT over an in-memory copy of those rows
  4. narrate  (LLM) turn the result into prose

port-overview skips 2 and 3 — it is already aggregated and small enough to
hand to the model directly.
"""

import json
import re
import sqlite3
import time
from datetime import date
from functools import wraps

from flask import jsonify, redirect, render_template, request, session, url_for

from .. import bp
from ..custom_report.views import fetch_source_rows
from . import ollama, sandbox, sources

MAX_NARRATE_ROWS = 100
MAX_NARRATE_CHARS = 4000
CHART_TYPES = ['bar', 'line', 'pie', 'doughnut', 'scatter', 'none']

_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Not logged in'}), 401
        return f(*args, **kwargs)
    return decorated


ROUTE_SCHEMA = {
    'type': 'object',
    'properties': {
        'source':    {'type': 'string', 'enum': sources.ALL_SOURCES},
        'date_col':  {'type': 'string'},
        'period':    {'type': 'string', 'enum': sources.PERIODS},
        'from_date': {'type': 'string'},
        'to_date':   {'type': 'string'},
    },
    'required': ['source', 'date_col', 'period'],
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


# -- stage 1: route ----------------------------------------------------------

def route(question, history_msgs, cfg, want_source=None, want_period=None):
    """Pick source + date column + period.

    When the user has chosen both from the UI there is nothing to infer, so the
    whole LLM call is skipped. When they chose one, the JSON schema is narrowed
    to that single value, so constrained decoding cannot override them.
    """
    if want_source and want_period:
        return _settle(want_source, None, want_period, {})

    schema = dict(ROUTE_SCHEMA, properties=dict(ROUTE_SCHEMA['properties']))
    if want_source:
        schema['properties']['source'] = {'type': 'string', 'enum': [want_source]}
    if want_period:
        schema['properties']['period'] = {'type': 'string', 'enum': [want_period]}

    system = (
        "You route a question about port operations to exactly one data source.\n"
        "Today is " + date.today().isoformat() + ".\n\n"
        "Sources:\n" + sources.catalog_prompt() + "\n\n"
        + sources.ROUTING_HINTS + "\n\n"
        "Rules:\n"
        "- Pick the source whose key columns can actually answer the question. If the "
        "question names a cargo type, the source must have a Cargo Type column.\n"
        "- date_col must come from the chosen source's list.\n"
        "- period must be one of: " + ', '.join(sources.PERIODS) + ". Use custom only "
        "when the question names explicit dates, and then also set from_date and to_date "
        "as YYYY-MM-DD.\n"
        "- If the question states no period, use this_fy."
    )
    msgs = [{'role': 'system', 'content': system}] + history_msgs + \
           [{'role': 'user', 'content': question}]
    out = json.loads(ollama.chat(msgs, cfg=cfg, schema=schema, num_predict=120))
    return _settle(out.get('source'), out.get('date_col'),
                   out.get('period'), out)


def _settle(src, date_col, period, out):
    """Validate a routing decision and turn its named period into real dates."""
    if not sources.is_valid(src):
        raise ValueError('Not a trusted data source: %r' % (src,))

    if src == sources.PORT_OVERVIEW:
        return {'source': src, 'date_col': '', 'period': 'today',
                'from_date': '', 'to_date': ''}

    if date_col not in sources.valid_date_cols(src):
        date_col = sources.default_date_col(src)

    # Python owns the date arithmetic. A 3B model gets "yesterday" and financial
    # year boundaries wrong often enough that an off-by-one silently returns the
    # wrong answer, and a named period is far easier for it to pick correctly.
    period = period or 'this_fy'
    if period == 'custom':
        frm, to = out.get('from_date', ''), out.get('to_date', '')
        if not _ISO.match(frm or '') or not _ISO.match(to or ''):
            raise ValueError('Model asked for a custom range but gave malformed dates')
        if frm > to:
            frm, to = to, frm
    else:
        if period not in sources.PERIODS:
            period = 'this_fy'
        frm, to = sources.resolve_period(period)
    return {'source': src, 'date_col': date_col, 'period': period,
            'from_date': frm, 'to_date': to}


# -- stage 3: sql ------------------------------------------------------------

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


# -- stage 4: narrate --------------------------------------------------------

def _as_table(cols, rows):
    out = [' | '.join(cols)]
    for r in rows[:MAX_NARRATE_ROWS]:
        out.append(' | '.join('' if v is None else str(v) for v in r))
    return '\n'.join(out)[:MAX_NARRATE_CHARS]


def narrate(question, history_msgs, context, cfg):
    system = (
        'You are a port operations analyst. Answer the question using ONLY the data below.\n\n'
        'How to answer:\n'
        '- Lead with the number: "IBRM discharge was 42,150 MT." Never open with '
        '"Based on the data provided" or "According to".\n'
        '- One to three sentences. No preamble, no restating the question, no offering '
        'further help at the end.\n'
        '- Quantities are metric tonnes (MT) unless the column name says otherwise. '
        'mbc-tat durations are minutes: convert anything over 120 into hours.\n'
        '- Format thousands with commas.\n'
        '- Add one comparison only when the data shows it: the largest contributor, or '
        'the direction of travel across periods.\n'
        '- Never reproduce the table or list it row by row. The reader already has it '
        'on screen. Summarise instead: the total, the top two or three, and anything '
        'odd such as rows with no value.\n'
        '- If the table below has rows, answer from them. Only say there is no data '
        'when the table is genuinely empty, and then say what was searched and what '
        'to try instead. Do not apologise.\n'
        '- Never state a number that is not in the data, and never estimate.\n\n'
        + context
    )
    msgs = [{'role': 'system', 'content': system}] + history_msgs + \
           [{'role': 'user', 'content': question}]
    return ollama.chat(msgs, cfg=cfg, num_predict=350).strip()


# -- port-overview path ------------------------------------------------------

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


# -- endpoints ---------------------------------------------------------------

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated


@bp.route('/module/RP01/ai-chat/')
@admin_required
def ai_chat_index():
    """Internal test page. Deliberately not linked from rp01.html, and admin
    gated so being unlinked is not the only thing keeping users out."""
    return render_template('ai_chat/ai_chat.html', username=session.get('username'))


@bp.route('/api/module/RP01/ai-chat/sources')
@login_required
def ai_chat_sources():
    return jsonify({
        'sources': [{'key': k, 'description': sources.DESCRIPTIONS[k]}
                    for k in sources.ALL_SOURCES],
        'periods': sources.PERIODS,
        'catalog': sources.catalog_prompt(),
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

    want_source = (body.get('source') or '').strip() or None
    want_period = (body.get('period') or '').strip() or None
    if want_source and not sources.is_valid(want_source):
        return jsonify({'error': 'Unknown data source: %s' % want_source}), 400
    if want_period and want_period not in sources.PERIODS:
        return jsonify({'error': 'Unknown period: %s' % want_period}), 400

    history, history_trimmed = trim_history(body.get('history'), cfg['max_history_chars'])
    timing = {}
    source_rows = None
    result = {'chart': None, 'chart_error': None, 'sql': None,
              'columns': [], 'rows': [], 'retried': False}

    try:
        t = time.time()
        picked = route(question, history, cfg, want_source, want_period)
        timing['route'] = round(time.time() - t, 2)
        routed_by = 'user' if (want_source and want_period) else 'model'

        if picked['source'] == sources.PORT_OVERVIEW:
            t = time.time()
            snapshot = _fetch_overview()
            timing['fetch'] = round(time.time() - t, 2)
            # ponytail: prose only for the live dashboard - it is already
            # aggregated, so there is nothing for SQL or a chart to add.
            context = ('Live port dashboard snapshot:\n'
                       + json.dumps(snapshot, default=str)[:MAX_NARRATE_CHARS])
            row_count = None
        else:
            t = time.time()
            rows = fetch_source_rows(picked['source'], picked['date_col'],
                                     picked['from_date'], picked['to_date'])
            timing['fetch'] = round(time.time() - t, 2)
            source_rows = len(rows)

            if not rows:
                return jsonify(dict(
                    picked,
                    answer=('No %s records with a %s between %s and %s. Try a wider period '
                            'or a different source.' % (picked['source'], picked['date_col'],
                                                        picked['from_date'], picked['to_date'])),
                    row_count=0, rows=[], columns=[], chart=None, chart_error=None,
                    sql=None, sql_retried=False, source_rows=0, timing=timing,
                    routed_by=routed_by, history_trimmed=history_trimmed))
            if len(rows) > cfg['max_rows']:
                return jsonify(dict(
                    picked, timing=timing,
                    error='That range returns %s rows (limit %s). Ask again with a '
                          'narrower date range.' % (format(len(rows), ','),
                                                    format(cfg['max_rows'], ',')))), 413

            conn, ddl = sandbox.load(rows)
            try:
                t = time.time()
                result = generate_and_run(question, history, conn, ddl, rows, cfg)
                timing['sql'] = round(time.time() - t, 2)
            finally:
                conn.close()

            context = ('Result of the query below, %d rows from %s (%s to %s):\n'
                       % (len(result['rows']), picked['source'],
                          picked['from_date'], picked['to_date'])
                       + _as_table(result['columns'], result['rows']))
            row_count = len(result['rows'])

        t = time.time()
        answer = narrate(question, history, context, cfg)
        timing['narrate'] = round(time.time() - t, 2)

    except Exception as e:
        return jsonify({'error': '%s: %s' % (type(e).__name__, e), 'timing': timing}), 502

    return jsonify(dict(
        picked,
        answer=answer,
        sql=result['sql'],
        sql_retried=result['retried'],
        columns=result['columns'],
        rows=result['rows'],
        row_count=row_count,
        chart=result['chart'],
        chart_error=result['chart_error'],
        source_rows=source_rows,
        routed_by=routed_by,
        history_trimmed=history_trimmed,
        timing=timing,
    ))
