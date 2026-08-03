"""Billing lock: an operational doc freezes once its cargo is billed, and
permanently once invoiced. Drives source_lock_state with a fake cursor that
answers each probe in order. Run: python test_billing_lock.py"""
from modules.FIN01.model import source_lock_state, lock_message


class FakeCursor:
    """Answers each execute() with the next scripted row. A row of None means
    'no match'. Records the SQL so we can assert which probe ran."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sql = []
        self._row = None

    def execute(self, sql, params=None):
        self.sql.append(' '.join(sql.split()))
        self._row = self.answers.pop(0) if self.answers else None

    def fetchone(self):
        return self._row


def test_unbilled_is_open():
    # MBC probes: 1 invoiced check, 1 billed check — both miss.
    cur = FakeCursor([None, None])
    assert source_lock_state(cur, 'MBC', 42) == ''
    assert lock_message('') == ''


def test_billed_blocks():
    cur = FakeCursor([None, {'?column?': 1}])
    assert source_lock_state(cur, 'MBC', 42) == 'billed'
    assert 'Remove or reject the bill' in lock_message('billed', 'MBC')


def test_invoiced_blocks_permanently():
    # First probe hits, so the billed probe never runs.
    cur = FakeCursor([{'?column?': 1}])
    assert source_lock_state(cur, 'MBC', 42) == 'invoiced'
    assert 'permanent' in lock_message('invoiced', 'MBC')
    assert 'invoice_bill_mapping' in cur.sql[0]
    assert len(cur.sql) == 1


def test_invoiced_ignores_bill_status():
    # The invoiced probe must not filter on bill_status — a cancelled bill
    # still means a customer was once invoiced off this cargo.
    cur = FakeCursor([{'?column?': 1}])
    source_lock_state(cur, 'MBC', 42)
    assert 'bill_status' not in cur.sql[0]


def test_vcn_checks_both_import_and_export():
    # VCN has two declaration tables; an unbilled call probes all four times.
    cur = FakeCursor([None, None, None, None])
    assert source_lock_state(cur, 'VCN', 7) == ''
    joined = ' '.join(cur.sql)
    assert 'vcn_cargo_declaration' in joined
    assert 'vcn_export_cargo_declaration' in joined


def test_export_only_billing_still_locks():
    # Import clean, export billed → locked.
    cur = FakeCursor([None, None, None, {'?column?': 1}])
    assert source_lock_state(cur, 'VCN', 7) == 'billed'


def test_unknown_kind_and_missing_id_are_open():
    assert source_lock_state(FakeCursor([]), 'BARGE', 1) == ''
    assert source_lock_state(FakeCursor([]), 'MBC', None) == ''


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
    print('billing lock: OK')
