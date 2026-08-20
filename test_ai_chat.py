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


# ── triage ─────────────────────────────────────────────────────────────────────

def _stub_chat(monkeypatch, *payloads):
    calls = iter(payloads)

    def fake(messages, cfg=None, schema=None, model=None, **kw):
        return next(calls)
    monkeypatch.setattr(aiviews.llm, 'chat', fake)


# ── SQL self-correction ──────────────────────────────────────────────────────

def test_bad_sql_is_retried_once_with_the_error(monkeypatch):
    seen = []

    def fake(messages, cfg=None, schema=None, model=None, **kw):
        seen.append(messages)
        if len(seen) == 1:
            return json.dumps({'sql': 'SELECT "Nope" FROM data',
                               'chart': {'type': 'none', 'x': '', 'y': [], 'title': ''}})
        return json.dumps({'sql': 'SELECT SUM("BL Qty") AS total FROM data',
                           'chart': {'type': 'none', 'x': '', 'y': [], 'title': ''}})
    monkeypatch.setattr(aiviews.llm, 'chat', fake)

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

    def fake(messages, cfg=None, schema=None, model=None, **kw):
        used.append(model)
        return json.dumps({'sql': 'SELECT 1 AS n FROM data LIMIT 1',
                           'chart': {'type': 'none', 'x': '', 'y': [], 'title': ''}})
    monkeypatch.setattr(aiviews.llm, 'chat', fake)
    conn, ddl = sb.load(ROWS)
    aiviews.generate_and_run('q', [], conn, ddl, ROWS, dict(CFG, sql_model='coder:7b'))
    assert used == ['coder:7b']


# ── catalog ──────────────────────────────────────────────────────────────────

def test_only_lueu_equipment_is_queryable():
    assert sources.ALL_SOURCES == ['lueu-equipment']
    assert sources.ONLY_SOURCE == 'lueu-equipment'
    for gone in ('mbc-ops', 'mbc-tat', 'vessel-barge', 'vessel-ops', 'port-overview'):
        assert not sources.is_valid(gone)


def test_catalog_describes_the_one_source_with_its_date_col():
    text = sources.catalog_prompt()
    assert 'lueu-equipment' in text and 'entry_date' in text
    assert 'mbc-ops' not in text


# ── triage ────────────────────────────────────────────────────────────────────

def test_triage_resolves_the_period_itself(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'in_scope': True, 'needs_query': True, 'period': 'yesterday'}))
    out = aiviews.triage('what did BUL-01 handle yesterday', [], CFG)
    from datetime import date as _d, timedelta
    y = (_d.today() - timedelta(days=1)).isoformat()
    assert out['from_date'] == y and out['to_date'] == y
    assert out['source'] == 'lueu-equipment'
    assert out['date_col'] == 'entry_date'
    assert out['needs_query'] is True


def test_triage_can_skip_the_query_entirely(monkeypatch):
    """A dashboard question must not pay for the SQL stage."""
    _stub_chat(monkeypatch, json.dumps({
        'in_scope': True, 'needs_query': False, 'period': 'today'}))
    assert aiviews.triage('how are we doing against target', [], CFG)['needs_query'] is False


def test_triage_refuses_off_topic(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'in_scope': False, 'needs_query': False, 'period': 'today'}))
    with pytest.raises(aiviews.OutOfScope):
        aiviews.triage('write me a python script', [], CFG)


def test_missing_in_scope_is_treated_as_in_scope(monkeypatch):
    """Only an explicit false refuses; a silent model must not lock users out."""
    _stub_chat(monkeypatch, json.dumps({'needs_query': True, 'period': 'today'}))
    assert aiviews.triage('q', [], CFG)['needs_query'] is True


def test_user_period_pins_the_schema_enum(monkeypatch):
    seen = {}

    def fake(messages, cfg=None, schema=None, model=None, **kw):
        seen['schema'] = schema
        return json.dumps({'in_scope': True, 'needs_query': True, 'period': 'today'})
    monkeypatch.setattr(aiviews.llm, 'chat', fake)

    out = aiviews.triage('q', [], CFG, want_period='last_month')
    assert seen['schema']['properties']['period']['enum'] == ['last_month']
    assert out['period'] == 'last_month'
    assert aiviews.TRIAGE_SCHEMA['properties']['period']['enum'] == sources.PERIODS


def test_triage_rejects_malformed_custom_dates(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'in_scope': True, 'needs_query': True, 'period': 'custom',
        'from_date': 'last April', 'to_date': '2026-06-30'}))
    with pytest.raises(ValueError, match='malformed dates'):
        aiviews.triage('q', [], CFG)


def test_triage_swaps_a_reversed_custom_range(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'in_scope': True, 'needs_query': True, 'period': 'custom',
        'from_date': '2026-06-30', 'to_date': '2026-04-01'}))
    out = aiviews.triage('q', [], CFG)
    assert out['from_date'] == '2026-04-01' and out['to_date'] == '2026-06-30'


# ── fixed context ────────────────────────────────────────────────────────────

OVERVIEW = {
    'as_of': '2026-08-20 14:00:00',
    'cargo_cards': {
        'today': {'label': 'Today', 'total': 12345.0, 'target': 15000,
                  'by_type': {'IBRM': 8000.0, 'CBRM': 4345.0},
                  'by_shift': {'A': 7000.0, 'B': 5345.0}},
        'current_fy': {'label': 'FY 2026-27', 'total': 1240500.0,
                       'target': 4000000, 'by_type': {'IBRM': 900000.0}},
    },
    'berths': [{'berth': 'BERTH 8', 'assets': [{'name': 'JSW SURYAGAD'}]},
               {'berth': 'BERTH 9', 'assets': []}],
    'delays': [{'delay': 'Crane breakdown', 'minutes': 120}],
    'mbc_status': [{'mbc_name': 'X', 'mbc_status': 'Discharging'}],
    'shift_incharge': 'A. Patel',
    'upcoming': [], 'notes': '', 'weather': {'temp': 31},
}
TARGETS = {'financial_year': '2026-27', 'fy_base_target': 4000000,
           'fy_effective_target': 4200000,
           'monthly': [{'month': 'April', 'base': 300000, 'outlook': 320000},
                       {'month': 'May', 'base': 350000, 'outlook': None}]}


def test_fixed_context_states_throughput_targets_and_berths():
    text = aiviews.render_fixed_context({'overview': OVERVIEW, 'targets': TARGETS})
    assert 'Today: 12,345 MT | target 15,000' in text
    assert 'IBRM 8,000' in text
    assert 'base 4,000,000 MT, effective 4,200,000 MT' in text
    assert 'Apr 300,000/outlook 320,000' in text
    assert 'BERTH 8' in text
    assert 'BERTH 9' not in text          # empty berths are noise
    assert 'A. Patel' in text


def test_fixed_context_is_capped():
    big = dict(OVERVIEW, mbc_status=[{'mbc_name': 'M%d' % i} for i in range(500)])
    text = aiviews.render_fixed_context({'overview': big, 'targets': TARGETS})
    assert len(text) <= aiviews.MAX_FIXED_CHARS


def test_fixed_context_survives_missing_pieces():
    assert aiviews.render_fixed_context({}) .startswith('LIVE PORT DASHBOARD')
    assert aiviews.render_fixed_context({'overview': {}, 'targets': {}})


def test_fixed_context_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(aiviews, '_fetch_overview', lambda: (calls.append(1), OVERVIEW)[1])
    monkeypatch.setattr(aiviews, '_fetch_targets', lambda: TARGETS)
    aiviews._cache.update(at=0.0, data=None)
    aiviews.fixed_context()
    aiviews.fixed_context()
    assert len(calls) == 1
    aiviews._cache.update(at=0.0, data=None)


# ── chart column matching ────────────────────────────────────────────────────

def test_chart_accepts_the_quoted_names_the_sql_rules_encourage():
    """The model is told to double-quote columns for SQL and carries the quotes
    into the chart spec; punctuation must not cost us the chart."""
    chart, err = aiviews.validate_chart(
        {'type': 'bar', 'x': '"Equipment"', 'y': ['"Total Quantity"'], 'title': 't'},
        ['Equipment', 'Total Quantity'])
    assert err is None
    assert chart['x'] == 'Equipment' and chart['y'] == ['Total Quantity']


def test_chart_matches_case_insensitively():
    chart, _ = aiviews.validate_chart(
        {'type': 'bar', 'x': 'equipment', 'y': ['TOTAL QUANTITY'], 'title': 't'},
        ['Equipment', 'Total Quantity'])
    assert chart['x'] == 'Equipment' and chart['y'] == ['Total Quantity']


def test_chart_drops_the_x_column_from_y_and_dedupes():
    chart, _ = aiviews.validate_chart(
        {'type': 'bar', 'x': 'Equipment', 'y': ['Equipment', 'Total', '"Total"'], 'title': 't'},
        ['Equipment', 'Total'])
    assert chart['y'] == ['Total']


def test_chart_still_rejects_a_name_that_is_not_there():
    chart, err = aiviews.validate_chart(
        {'type': 'bar', 'x': '"Vessel"', 'y': ['"Total"'], 'title': 't'},
        ['Equipment', 'Total'])
    assert chart is None and 'x-axis' in err


# ── single-value answers ─────────────────────────────────────────────────────

def test_single_value_is_stated_even_if_the_model_omits_it():
    assert aiviews.ensure_answer('', ['Total MT'], [[35]]) == 'Total MT: 35.'
    assert aiviews.ensure_answer('The total has been calculated.',
                                 ['Total MT'], [[35.0]]).startswith('Total MT: 35.')


def test_single_value_answer_left_alone_when_the_number_is_there():
    assert aiviews.ensure_answer('Total was 35 MT.', ['Total MT'], [[35]]) == 'Total was 35 MT.'


def test_single_value_formats_thousands():
    assert aiviews.ensure_answer('', ['Total MT'], [[1240500]]) == 'Total MT: 1,240,500.'


def test_null_single_value_says_so():
    assert 'empty' in aiviews.ensure_answer('', ['Total MT'], [[None]])


def test_multi_row_answers_are_not_rewritten():
    assert aiviews.ensure_answer('BUL-05 led.', ['a', 'b'], [[1, 2], [3, 4]]) == 'BUL-05 led.'


# ── period resolution ────────────────────────────────────────────────────────

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


# ── column knowledge ─────────────────────────────────────────────────────────

def test_delay_is_a_label_not_a_measure():
    measures, labels = sources.columns_for('lueu-equipment')
    assert 'Delay' in labels and 'Delay' not in measures
    assert measures == ['Quantity', 'Diff Hrs']


def test_seen_columns_correct_a_stale_catalog():
    original = dict(sources._seen_columns)
    try:
        sources.remember_columns('lueu-equipment',
                                 ['Equipment', 'Quantity', 'Brand New Column', '_from_time'])
        measures, labels = sources.columns_for('lueu-equipment')
        assert measures == ['Quantity']            # Diff Hrs gone from the result
        assert 'Brand New Column' in labels        # picked up automatically
        assert '_from_time' not in labels          # internal columns stay hidden
        assert 'Operator' not in labels            # no longer in the real result
    finally:
        sources._seen_columns.clear()
        sources._seen_columns.update(original)


# ── read-only connection ─────────────────────────────────────────────────────

def test_no_readonly_user_means_the_normal_connection():
    assert aiviews.readonly_connection({}) is None
    assert aiviews.readonly_connection({'db_user': '  '}) is None


# ── chart forms ──────────────────────────────────────────────────────────────

def test_all_rendered_chart_forms_are_offered_to_the_model():
    """The schema enum and the renderer must not drift apart."""
    for kind in ('kpi', 'bar', 'hbar', 'stacked_bar', 'line', 'area',
                 'pie', 'doughnut', 'scatter', 'none'):
        assert kind in aiviews.CHART_TYPES
    assert aiviews.SQL_SCHEMA['properties']['chart']['properties']['type']['enum'] \
        == aiviews.CHART_TYPES


def test_kpi_needs_no_label_column():
    chart, err = aiviews.validate_chart(
        {'type': 'kpi', 'x': '', 'y': ['Total MT'], 'title': 't'}, ['Total MT'])
    assert err is None and chart['type'] == 'kpi' and chart['x'] is None


def test_kpi_still_needs_a_real_value_column():
    chart, err = aiviews.validate_chart(
        {'type': 'kpi', 'x': '', 'y': ['Nope'], 'title': 't'}, ['Total MT'])
    assert chart is None and 'y column' in err


def test_non_kpi_forms_still_require_a_label_column():
    for kind in ('bar', 'hbar', 'stacked_bar', 'area'):
        chart, err = aiviews.validate_chart(
            {'type': kind, 'x': '', 'y': ['Total'], 'title': 't'}, ['Equipment', 'Total'])
        assert chart is None and 'x-axis' in err


def test_models_used_flags_a_two_model_split():
    assert aiviews._models_used({'model': 'q', 'sql_model': ''})['split'] is False
    assert aiviews._models_used({'model': 'q', 'sql_model': 'q'})['split'] is False
    split = aiviews._models_used({'model': 'llama3.2:3b', 'sql_model': 'qwen2.5-coder:7b'})
    assert split['split'] is True
    assert split['triage'] == 'llama3.2:3b' and split['sql'] == 'qwen2.5-coder:7b'


# ── needs_query safety net ───────────────────────────────────────────────────

def test_record_questions_query_even_when_the_model_says_no():
    """The dashboard holds cargo-type totals only. Answering an equipment
    question from it substitutes one for the other and states it as fact."""
    for q in ['make a pie chart of equipments by quantity handled',
              'which operator handled the most last month',
              'total hours by shift',
              'most common delay types',
              'how much did BUL-05 handle',
              'crane utilisation this month',
              'berth wise breakdown']:
        assert aiviews.needs_records(q, False) is True, q


def test_pure_dashboard_questions_still_skip_the_query():
    for q in ['how are we tracking against this years target',
              'what is todays throughput',
              'are we ahead of the monthly target']:
        assert aiviews.needs_records(q, False) is False, q


def test_a_model_yes_is_always_honoured():
    assert aiviews.needs_records('anything at all', True) is True


def test_generic_date_words_do_not_force_a_query():
    """Year and Date are real columns but every question mentions one."""
    assert aiviews.needs_records('target for this year', False) is False
    assert aiviews.needs_records('throughput to date', False) is False


def test_triage_applies_the_override(monkeypatch):
    _stub_chat(monkeypatch, json.dumps({
        'in_scope': True, 'needs_query': False, 'period': 'this_month'}))
    out = aiviews.triage('pie chart of equipment by quantity', [], CFG)
    assert out['needs_query'] is True


# ── arithmetic is Python's job ───────────────────────────────────────────────

def test_percentages_are_computed_not_left_to_the_model():
    """A 3B asked to divide 24,197 by 25,004,386 answered 4.3%."""
    assert aiviews._pct(24197, 25004386) == '0.1'        # the model said 4.3%
    assert aiviews._pct(1240500, 4000000) == '31.0'
    assert aiviews._pct(50, 100) == '50.0'


def test_pct_refuses_meaningless_input():
    assert aiviews._pct(5, 0) is None
    assert aiviews._pct(None, 100) is None
    assert aiviews._pct('abc', 100) is None


def test_card_line_states_progress_against_target():
    line = aiviews._card_line({'total': 12345, 'target': 15000,
                               'by_type': {'IBRM': 12345}})
    assert '12,345 MT' in line
    assert 'target 15,000' in line
    assert '82.3% of that target' in line


def test_fy_progress_is_stated_so_nothing_has_to_divide():
    text = aiviews.render_fixed_context({
        'overview': {'as_of': 'now',
                     'cargo_cards': {'current_fy': {'label': 'FY 2026-27',
                                                    'total': 1240500}},
                     'berths': []},
        'targets': {'financial_year': '2026-27', 'fy_base_target': 25000000,
                    'fy_effective_target': 25004386, 'monthly': []}})
    assert 'FY progress: 1,240,500 MT delivered, 4.96% of the effective target' in text


def test_narration_forbids_arithmetic():
    import inspect
    src = inspect.getsource(aiviews.narrate)
    assert 'Do NOT do arithmetic' in src
    assert 'never a percentage of a' in src


# ── provider switch ──────────────────────────────────────────────────────────

import modules.RP01.RP01.ai_chat.llm as llm


def test_provider_defaults_to_self_hosted():
    assert llm.provider({}) == llm.OLLAMA
    assert llm.provider({'provider': 'ollama'}) == llm.OLLAMA
    assert llm.provider({'provider': 'gemini'}) == llm.GEMINI
    assert llm.provider({'provider': 'nonsense'}) == llm.OLLAMA


def test_gemini_schema_conversion():
    """Gemini wants uppercase types and rejects keys it does not know."""
    out = llm.to_gemini_schema(aiviews.TRIAGE_SCHEMA)
    assert out['type'] == 'OBJECT'
    assert out['properties']['in_scope']['type'] == 'BOOLEAN'
    assert out['properties']['period']['enum'] == sources.PERIODS
    assert out['required'] == ['in_scope', 'needs_query', 'period']


def test_gemini_schema_handles_arrays():
    out = llm.to_gemini_schema(aiviews.SQL_SCHEMA)
    y = out['properties']['chart']['properties']['y']
    assert y['type'] == 'ARRAY' and y['items']['type'] == 'STRING'


def test_gemini_schema_drops_unsupported_keys():
    out = llm.to_gemini_schema({'type': 'string', 'default': 'x', 'minLength': 2})
    assert out == {'type': 'STRING'}


def test_gemini_messages_split_out_the_system_prompt():
    system, contents = llm.to_gemini_messages([
        {'role': 'system', 'content': 'you are an analyst'},
        {'role': 'user', 'content': 'hello'},
        {'role': 'assistant', 'content': 'hi'},
        {'role': 'user', 'content': 'again'},
    ])
    assert system == 'you are an analyst'
    assert [c['role'] for c in contents] == ['user', 'model', 'user']
    assert contents[0]['parts'][0]['text'] == 'hello'


def test_gemini_reply_reading():
    ok = {'candidates': [{'content': {'parts': [{'text': 'a'}, {'text': 'b'}]}}]}
    assert llm.read_gemini_reply(ok) == 'ab'


def test_gemini_empty_reply_says_why():
    with pytest.raises(ValueError, match='MAX_TOKENS'):
        llm.read_gemini_reply({'candidates': [{'finishReason': 'MAX_TOKENS',
                                               'content': {'parts': []}}]})
    with pytest.raises(ValueError, match='blocked: SAFETY'):
        llm.read_gemini_reply({'promptFeedback': {'blockReason': 'SAFETY'}})


def test_gemini_without_a_key_fails_clearly():
    with pytest.raises(ValueError, match='no API key'):
        llm.chat([{'role': 'user', 'content': 'x'}],
                 cfg=dict(llm.DEFAULTS, provider='gemini', gemini_api_key=''))


def test_models_used_reports_the_provider():
    g = aiviews._models_used(dict(llm.DEFAULTS, provider='gemini',
                                  gemini_model='gemini-2.5-pro'))
    assert g['provider'] == 'gemini' and g['sql'] == 'gemini-2.5-pro'
    assert g['split'] is False
    o = aiviews._models_used(dict(llm.DEFAULTS, provider='ollama'))
    assert o['provider'] == 'ollama'
