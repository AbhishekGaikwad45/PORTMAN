# RP01 AI Chat — Design

**Date:** 2026-08-19
**Status:** Implemented (API only; no RP01 front end yet)

## Goal

Answer natural-language questions about port performance using a self-hosted
Ollama server, returning prose plus a chart spec, without letting a language
model near production SQL.

## Why this shape

The Custom Report designer already defines seven curated data sources whose
queries return **flat rows with human-readable column names** (`"Cargo Type"`,
`"BL Qty (MT)"`, `"TAT (min)"`). That is the semantic layer that normally sinks
text-to-SQL projects, and it already exists. The design reuses it rather than
exposing the raw Postgres schema.

Consequence for accuracy: the model sees one flat table with ~25 plain-English
columns, not a multi-table warehouse schema. On BIRD (messy multi-table),
Qwen2.5-Coder-7B scores 39%. On Spider (single clean schema) the same model
scores ~82%. Reducing the task to one flat table is what makes small models
viable here.

## Flow

`POST /api/module/RP01/ai-chat/ask` — `{question, history[]}`

| Stage | What | Model |
|---|---|---|
| 1. route | pick source + date column + ISO range | `model` |
| 2. fetch | run the existing `fetch_source_rows` query | — |
| 3. sql | write one SELECT over an in-memory copy | `sql_model` or `model` |
| 4. narrate | turn the result into prose | `model` |

`port-overview` skips stages 2 and 3 — the dashboard payload is already
aggregated and small enough to hand to the model directly, so it is prose-only
with no chart.

All structured stages use Ollama's `format` with a real JSON Schema.
Constrained decoding makes malformed JSON mechanically impossible, pins
`source` to an enum, and is markedly faster than free-form generation because
the model spends no tokens on formatting decisions.

## Security model

The model's SQL never touches Postgres. It runs against a throwaway
`sqlite3.connect(':memory:')` copy of rows the caller already had permission to
read. Three guards:

1. `conn.set_authorizer()` — denies everything except `SELECT`, `FUNCTION`,
   `RECURSIVE`, and `READ` on the single `data` table. This is what closes
   `ATTACH DATABASE` reaching the filesystem, and it blocks reads of
   `sqlite_master`.
2. `sandbox.run()` rejects anything not starting `SELECT`/`WITH`, and rejects
   stacked statements.
3. `SQLITE_DBCONFIG_DQS_DML` / `_DDL` disabled. **Non-obvious and important:**
   SQLite's legacy fallback treats an unresolvable `"double-quoted"` token as a
   string literal, so `SELECT "Nope" FROM data` returns the word `Nope` for
   every row instead of erroring. Left on, a hallucinated column name produces
   fake data *and* skips the retry. Regression-tested.

**Ollama has no authentication.** The configured host must be reachable only
from the app server.

## Correctness measures

- **One self-correction retry.** A SQLite error is fed back to the model once,
  then the request fails. Recommended by the BIRD on-prem study as the cheapest
  available accuracy gain.
- **Column type sniffing.** Numeric columns become `INTEGER`/`REAL`; left as
  `TEXT`, sqlite sorts `'1000'` below `'9'` and `ORDER BY` on a quantity
  silently returns the wrong rows.
- **Chart spec validation.** The model returns `{type, x, y[], title}`, never
  raw Chart.js config. `x` and `y` must be columns present in the result, so a
  hallucinated name fails visibly instead of rendering an empty chart.
- **Always show the work.** Every response carries `source`, `date_col`,
  `from_date`, `to_date`, `sql`, `sql_retried`, `source_rows`, `row_count` and
  per-stage `timing`. These numbers reach management reports; an unverifiable
  number is worse than no number. The exposed SQL is also how you evaluate a
  candidate model.
- **Row caps.** `max_rows` (source → sqlite, default 50k) returns HTTP 413 with
  a "narrow the date range" message rather than truncating silently.
  `max_result_rows` caps what is returned; narration sees at most 100 rows /
  4000 chars.

## Conversation history

Full history is kept until `max_history_chars`, then oldest turns drop and
`history_trimmed: true` is returned. The context window is a hard limit, not a
preference — on CPU a long thread costs prompt ingestion on every turn.

## Model configuration

Stored in `module_config` under `AICHAT` via the existing generic
`/admin/api/config/<code>` endpoints. No migration, no new table.

Recommended starting point for a 48GB-RAM CPU-only box:

```
model      = llama3.2:3b        # ~2GB  — routing + narration
sql_model  = qwen2.5-coder:7b   # ~4.7GB — SQL only
```

Rationale: Llama 3.2 3B is strong for its size at structured output (67% BFCL
v2) and fast on CPU, but sub-4B models are weak at text-to-SQL, where a wrong
answer is a plausible number rather than an error. Splitting the stages puts
the capable model only on the call that needs it. Set both fields the same and
read the emitted SQL to decide whether the second model is needed.

`keep_alive` defaults to 30m — cold-loading a model per request dominates
everything else.

## Files

| Path | Role |
|---|---|
| `modules/RP01/RP01/ai_chat/sources.py` | 8 one-line source descriptions. Column names deliberately absent — read from the live result so it cannot drift. |
| `modules/RP01/RP01/ai_chat/ollama.py` | Config defaults + `/api/chat` and `/api/tags` wrapper. |
| `modules/RP01/RP01/ai_chat/sandbox.py` | Type sniffing, in-memory load, authorizer, guarded execute. Stdlib only. |
| `modules/RP01/RP01/ai_chat/views.py` | The four stages and the endpoints. |
| `modules/RP01/RP01/custom_report/views.py` | `fetch_source_rows` split out of the `pivot_data` view so it is callable internally. |
| `modules/ADMIN/views.py` | `/admin/api/aichat/test` — ping Ollama, list installed models. |
| `templates/admin.html` | AI Config tab. |
| `test_ai_chat.py` | 28 tests, no DB and no Ollama required. |

## Deliberate omissions

- **No RP01 chat UI.** Testing happens against the route first.
- **No SSE streaming.** Nothing consumes it yet; only stage 4 benefits. Add it
  with the UI.
- **No chart for `port-overview`.** Already aggregated; prose is the answer.
- **No retry on stage 1 routing.** Constrained enum decoding makes an invalid
  source impossible; a wrong-but-valid source is visible in the response.

## Known limits

- Three CPU inference calls stack: expect 20–45s per answer, less with the 3B
  on stages 1 and 4. A GPU is the fix, not a code change.
- Small models pick the right option less reliably as the list grows, and the
  routing list has 8 entries. If routing misfires in testing, merge the
  `lueu-*` and `vessel-*` sources into coarser buckets.
