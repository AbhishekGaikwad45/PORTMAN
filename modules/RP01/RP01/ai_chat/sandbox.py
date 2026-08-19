"""Throwaway in-memory SQLite holding one source's rows.

The model's SQL never touches Postgres. It runs against a private copy of
rows the caller already had permission to read, in a database that is
discarded when the request ends. That removes the injection surface; the
authorizer below closes the one real escape left (ATTACH reaching the
filesystem, load_extension running code).
"""

import re
import sqlite3
import time

TABLE = 'data'

_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,   # CTEs
}

_SELECT_START = re.compile(r'^\s*(select|with)\b', re.I)


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and arg1 == TABLE:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def sniff_type(values):
    """INTEGER / REAL / TEXT from the values actually present.

    Typing matters: left as TEXT, sqlite sorts '1000' before '9' and
    ORDER BY on a quantity column silently returns the wrong rows.
    """
    seen = False
    is_int = True
    for v in values:
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 'TEXT'
        seen = True
        if isinstance(v, float) or (isinstance(v, str) and '.' in v) or f != int(f):
            is_int = False
    if not seen:
        return 'TEXT'
    return 'INTEGER' if is_int else 'REAL'


def _coerce(v, typ):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if typ == 'INTEGER':
        return int(float(v))
    if typ == 'REAL':
        return float(v)
    return v if isinstance(v, str) else str(v)


def load(rows):
    """Build the sandbox. Returns (conn, ddl). Authorizer is armed last so
    our own INSERTs still go through."""
    cols = list(rows[0].keys())
    types = {c: sniff_type([r.get(c) for r in rows]) for c in cols}
    conn = sqlite3.connect(':memory:')
    # SQLite's legacy fallback treats an unresolvable "double-quoted" token as a
    # string literal, so SELECT "Nope" FROM data quietly returns the word Nope for
    # every row instead of erroring. That would let a hallucinated column name
    # produce fake data and skip the self-correction retry entirely.
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
    ddl = 'CREATE TABLE {} (\n  {}\n)'.format(
        TABLE,
        ',\n  '.join('"{}" {}'.format(c.replace('"', '""'), types[c]) for c in cols),
    )
    conn.execute(ddl)
    conn.executemany(
        'INSERT INTO {} VALUES ({})'.format(TABLE, ','.join('?' * len(cols))),
        [[_coerce(r.get(c), types[c]) for c in cols] for r in rows],
    )
    conn.commit()
    conn.set_authorizer(_authorizer)
    return conn, ddl


def run(conn, sql, limit=5000, timeout_s=20):
    """Execute one read-only statement. Returns (columns, rows)."""
    sql = sql.strip().rstrip(';').strip()
    if not _SELECT_START.match(sql):
        raise ValueError('Only SELECT queries are allowed')
    if ';' in sql:
        raise ValueError('Only a single statement is allowed')

    deadline = time.time() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        out = [list(r) for r in cur.fetchmany(limit)]
    finally:
        conn.set_progress_handler(None, 0)
    return cols, out
