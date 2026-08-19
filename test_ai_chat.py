"""RP01 AI chat guards — sandbox isolation, typing, validation. No DB, no Ollama."""
import json
import sqlite3

import pytest

import modules.RP01.RP01.ai_chat.sandbox as sb
import modules.RP01.RP01.ai_chat.views as aiviews
import modules.RP01.RP01.ai_chat.sources as sources

ROWS = [
    {'Doc No': '1001', 'Cargo Type': 'IBRM', 'BL Qty': '9',    'Year-Month': '2026-04'},
    {'Doc No': '1002', 'Cargo Type': 'CBRM', 'BL Qty': '1000.5', 'Year-Month': '2026-05'},
    {'Doc No': '1003', 'Cargo Type': 'IBRM', 'BL Qty': '250',  'Year-Month': '2026-05'},
]

CFG = {'model': 'm', 'sql_model': '', 'max_result_rows': 5000, 'max_history_chars': 200}


# ── type sniffing ────────────────────────────────────────────────────────────

def test_sniff_types():
    assert sb.sniff_type(['1', '2', '3']) == 'INTEGER'
    assert sb.sniff_type(['1', '2.5']) == 'REAL'
    assert sb.sniff_type(['IBRM', 'CBRM']) == 'TEXT'
    assert sb.sniff_type(['2026-04-01']) == 'TEXT'
    assert sb.sniff_type([None, '', '  ']) == 'TEXT'
    assert sb.sniff_type(['5', None, '']) == 'INTEGER'


def test_numeric_columns_sort_numerically():
    """The whole point of typing: as TEXT, '1000.5' sorts below '250'."""
    conn, _ = sb.load(ROWS)
    _, out = sb.run(conn, 'SELECT "BL Qty" FROM data ORDER BY "BL Qty" DESC')
    assert [r[0] for r in out] == [1000.5, 250.0, 9.0]


def test_integer_columns_keep_integer_form():
    conn, ddl = sb.load(ROWS)
    assert '"Doc No" INTEGER' in ddl
    _, out = sb.run(conn, 'SELECT "Doc No" FROM data ORDER BY "Doc No"')
    assert out[0][0] == 1001


# ── sandbox isolation ────────────────────────────────────────────────────────

def test_select_and_group_by_work():
    conn, _ = sb.load(ROWS)
    cols, out = sb.run(
        conn, 'SELECT "Cargo Type", SUM("BL Qty") AS total FROM data GROUP BY 1 ORDER BY 2 DESC')
    assert cols == ['Cargo Type', 'total']
    assert out == [['CBRM', 1000.5], ['IBRM', 259.0]]


@pytest.mark.parametrize('sql', [
    "ATTACH DATABASE 'evil.db' AS evil",
    "INSERT INTO data VALUES (9, 'X', 1, 'z')",
    "UPDATE data SET \"BL Qty\" = 0",
    "DROP TABLE data",
    "SELECT name FROM sqlite_master",
])
def test_authorizer_denies_everything_but_reading_data(sql):
    """Even bypassing run()'s SELECT check, sqlite itself refuses."""
    conn, _ = sb.load(ROWS)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(sql)


@pytest.mark.parametrize('sql', [
    "INSERT INTO data VALUES (9, 'X', 1, 'z')",
    "DROP TABLE data",
    "  pragma table_info(data)",
])
def test_run_rejects_non_select(sql):
    conn, _ = sb.load(ROWS)
    with pytest.raises(ValueError, match='Only SELECT'):
        sb.run(conn, sql)


def test_run_rejects_stacked_statements():
    conn, _ = sb.load(ROWS)
    with pytest.raises(ValueError, match='single statement'):
        sb.run(conn, 'SELECT 1 FROM data; DROP TABLE data')


def test_run_allows_cte_and_trailing_semicolon():
    conn, _ = sb.load(ROWS)
    _, out = sb.run(conn, 'WITH t AS (SELECT "BL Qty" q FROM data) SELECT SUM(q) FROM t;')
    assert out == [[1259.5]]


# ── chart validation ─────────────────────────────────────────────────────────

def test_chart_rejects_hallucinated_x_column():
    chart, err = aiviews.validate_chart(
        {'type': 'bar', 'x': 'Nonexistent', 'y': ['total'], 'title': 't'}, ['Cargo Type', 'total'])
    assert chart is None and 'x-axis' in err


def test_chart_drops_unknown_y_columns():
    chart, err = aiviews.validate_chart(
        {'type': 'bar', 'x': 'Cargo Type', 'y': ['total', 'bogus'], 'title': 't'},
        ['Cargo Type', 'total'])
    assert chart['y'] == ['total'] and err is None


def test_chart_none_type_yields_no_chart():
    assert aiviews.validate_chart({'type': 'none', 'x': '', 'y': [], 'title': ''}, ['a']) == (None, None)


# ── history trimming ─────────────────────────────────────────────────────────

def test_history_kept_until_budget_then_oldest_dropped():
    history = [{'role': 'user', 'content': 'x' * 80} for _ in range(5)]
    msgs, trimmed = aiviews.trim_history(history, 200)
    assert trimmed is True
    assert len(msgs) == 2          # 2 * 80 fits in 200, a third would not
    msgs, trimmed = aiviews.trim_history(history[:2], 200)
    assert trimmed is False and len(msgs) == 2


# ── routing ──────────────────────────────────────────────────────────────────

def _stub_chat(monkeypatch, *payloads):
    calls = iter(payloads)

    def fake(messages, cfg=None, schema=None, model=None):
        return next(calls)
    monkeypatch.setattr(aiviews.ollama, 'chat', fake)


def test_route_falls_back_to_default_date_col(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'source': 'mbc-ops', 'date_col': 'not_a_column', 'period': 'this_fy',
        'from_date': '2026-04-01', 'to_date': '2026-06-30'}))
    out = aiviews.route('q', [], CFG)
    assert out['date_col'] == sources.default_date_col('mbc-ops')


def test_route_swaps_reversed_date_range(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'source': 'mbc-ops', 'date_col': 'doc_date', 'period': 'custom',
        'from_date': '2026-06-30', 'to_date': '2026-04-01'}))
    out = aiviews.route('q', [], CFG)
    assert out['from_date'] == '2026-04-01' and out['to_date'] == '2026-06-30'


def test_route_rejects_unknown_source(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'source': 'invoices', 'date_col': 'x', 'period': 'this_fy',
        'from_date': '2026-04-01', 'to_date': '2026-06-30'}))
    with pytest.raises(ValueError, match='not trusted'):
        aiviews.route('q', [], CFG)


def test_route_rejects_malformed_dates(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'source': 'mbc-ops', 'date_col': 'doc_date', 'period': 'custom',
        'from_date': 'last April', 'to_date': '2026-06-30'}))
    with pytest.raises(ValueError, match='malformed dates'):
        aiviews.route('q', [], CFG)


def test_route_ignores_dates_for_port_overview(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'source': 'port-overview', 'date_col': '', 'period': 'today',
        'from_date': '', 'to_date': ''}))
    assert aiviews.route('q', [], CFG)['source'] == 'port-overview'


# ── SQL self-correction ──────────────────────────────────────────────────────

def test_bad_sql_is_retried_once_with_the_error(monkeypatch):
    seen = []

    def fake(messages, cfg=None, schema=None, model=None):
        seen.append(messages)
        if len(seen) == 1:
            return json.dumps({'sql': 'SELECT "Nope" FROM data',
                               'chart': {'type': 'none', 'x': '', 'y': [], 'title': ''}})
        return json.dumps({'sql': 'SELECT SUM("BL Qty") AS total FROM data',
                           'chart': {'type': 'none', 'x': '', 'y': [], 'title': ''}})
    monkeypatch.setattr(aiviews.ollama, 'chat', fake)

    conn, ddl = sb.load(ROWS)
    out = aiviews.generate_and_run('total?', [], conn, ddl, ROWS, CFG)
    assert out['retried'] is True
    assert out['rows'] == [[1259.5]]
    assert 'no such column' in seen[1][-1]['content'].lower()


def test_sql_gives_up_after_one_retry(monkeypatch):
    _stub_chat(
        monkeypatch,
        json.dumps({'sql': 'SELECT "Nope" FROM data', 'chart': {'type': 'none', 'x': '', 'y': [], 'title': ''}}),
        json.dumps({'sql': 'SELECT "Still" FROM data', 'chart': {'type': 'none', 'x': '', 'y': [], 'title': ''}}),
    )
    conn, ddl = sb.load(ROWS)
    with pytest.raises(ValueError, match='after one retry'):
        aiviews.generate_and_run('q', [], conn, ddl, ROWS, CFG)


def test_sql_model_is_used_when_set(monkeypatch):
    used = []

    def fake(messages, cfg=None, schema=None, model=None):
        used.append(model)
        return json.dumps({'sql': 'SELECT 1 AS n FROM data LIMIT 1',
                           'chart': {'type': 'none', 'x': '', 'y': [], 'title': ''}})
    monkeypatch.setattr(aiviews.ollama, 'chat', fake)
    conn, ddl = sb.load(ROWS)
    aiviews.generate_and_run('q', [], conn, ddl, ROWS, dict(CFG, sql_model='coder:7b'))
    assert used == ['coder:7b']


# ── catalog ──────────────────────────────────────────────────────────────────

def test_every_source_has_a_one_line_description():
    assert len(sources.ALL_SOURCES) == 6
    for key in sources.ALL_SOURCES:
        desc = sources.DESCRIPTIONS[key]
        assert desc and '\n' not in desc


def test_catalog_lists_date_cols_for_pivot_sources():
    text = sources.catalog_prompt()
    assert 'mbc-ops' in text and 'doc_date' in text


# ── trusted sources & period resolution ──────────────────────────────────────

def test_untrusted_sources_are_rejected():
    """vessel-ops has no Cargo Type column, so it can only mislead."""
    for key in ('vessel-ops', 'vessel-anchorage'):
        assert not sources.is_valid(key)
        assert key not in sources.catalog_prompt()


def test_trusted_sources_are_the_lueu_barge_and_mbc_ones():
    assert sources.ALL_SOURCES == ['mbc-ops', 'mbc-tat', 'vessel-barge',
                                   'lueu-equipment', 'lueu-historical', 'port-overview']


def test_periods_resolve_against_a_fixed_today():
    from datetime import date as _d
    t = _d(2026, 8, 19)
    assert sources.resolve_period('today', t) == ('2026-08-19', '2026-08-19')
    assert sources.resolve_period('yesterday', t) == ('2026-08-18', '2026-08-18')
    assert sources.resolve_period('last_7_days', t) == ('2026-08-13', '2026-08-19')
    assert sources.resolve_period('this_month', t) == ('2026-08-01', '2026-08-19')
    assert sources.resolve_period('last_month', t) == ('2026-07-01', '2026-07-31')
    assert sources.resolve_period('this_fy', t) == ('2026-04-01', '2026-08-19')
    assert sources.resolve_period('last_fy', t) == ('2025-04-01', '2026-03-31')


def test_financial_year_starts_in_april():
    from datetime import date as _d
    assert sources.fy_start(_d(2026, 3, 31)) == _d(2025, 4, 1)
    assert sources.fy_start(_d(2026, 4, 1)) == _d(2026, 4, 1)


def test_route_resolves_named_period_itself(monkeypatch):
    """The model names a period; Python does the arithmetic."""
    _stub_chat(monkeypatch, json.dumps({
        'source': 'mbc-ops', 'date_col': 'doc_date', 'period': 'yesterday'}))
    out = aiviews.route('what was yesterdays IBRM discharge', [], CFG)
    from datetime import date as _d, timedelta
    y = (_d.today() - timedelta(days=1)).isoformat()
    assert out['from_date'] == y and out['to_date'] == y
    assert out['source'] == 'mbc-ops'
