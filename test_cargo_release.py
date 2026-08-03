"""Release Cargo: frees one declaration's billed tracking and nothing else.
The invariant that matters is what it must NOT touch — bills, invoices and SAP
postings stay as issued, because the money is corrected by a credit note.
Run: python test_cargo_release.py"""
from modules.FIN01 import model


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sql = []
        self.params = []
        self._row = None

    def execute(self, sql, params=None):
        normalized = ' '.join(sql.split())
        self.sql.append(normalized)
        self.params.append(params)
        # Only queries return rows — an UPDATE/INSERT must not eat a scripted one.
        if normalized.upper().startswith('SELECT'):
            self._row = self.rows.pop(0) if self.rows else None
        else:
            self._row = None

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _run(monkey_rows):
    cur = FakeCursor(monkey_rows)
    conn = FakeConn(cur)
    orig_db, orig_cursor = model.get_db, model.get_cursor
    model.get_db = lambda: conn
    model.get_cursor = lambda c: cur
    try:
        result = model.release_cargo('MBC', 1234, 'shubham', reason='wrong party')
    finally:
        model.get_db, model.get_cursor = orig_db, orig_cursor
    return result, cur, conn


def test_release_resets_tracking_and_audits():
    result, cur, conn = _run([
        {'customer_name': 'AMBA RIVER COKE LIMITED', 'cargo_name': 'Illawara Coal',
         'billed_quantity': 7816.0},
        {'bills': 'BILL0174', 'invoices': 'DPPL/26-27/0041'},
    ])
    assert result['ok'] is True
    assert result['cargo'] == 'Illawara Coal'
    assert conn.committed and not conn.rolled_back

    joined = ' '.join(cur.sql)
    # the three fields that make it billable again
    assert 'SET is_billed=0, billed_quantity=0, bill_id=NULL' in joined
    # audit row naming the documents left standing
    assert 'INSERT INTO approval_log' in joined
    comment = cur.params[-1][1]
    assert 'BILL0174' in comment and 'DPPL/26-27/0041' in comment
    assert 'wrong party' in comment
    assert '7816' in comment


def test_release_never_touches_bills_or_invoices():
    _, cur, _ = _run([
        {'customer_name': 'X', 'cargo_name': 'Y', 'billed_quantity': 1.0},
        {'bills': 'BILL0174', 'invoices': 'DPPL/26-27/0041'},
    ])
    writes = [s for s in cur.sql
              if s.startswith('UPDATE') or s.startswith('DELETE') or s.startswith('INSERT')]
    for sql in writes:
        assert 'bill_header' not in sql, sql
        assert 'bill_lines' not in sql, sql
        assert 'invoice_header' not in sql, sql
        assert 'invoice_lines' not in sql, sql
        assert 'invoice_bill_mapping' not in sql, sql
        assert 'sap_outbound_queue' not in sql, sql


def test_missing_declaration_is_refused():
    result, _, conn = _run([None])
    assert result['ok'] is False
    assert 'not found' in result['error'].lower()
    assert not conn.committed


def test_unknown_source_type_is_refused():
    result = model.release_cargo('BARGE', 1, 'shubham')
    assert result['ok'] is False
    assert 'Unknown cargo source type' in result['error']


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
    print('cargo release: OK')
