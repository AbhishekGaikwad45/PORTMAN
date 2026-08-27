"""Service records bill partially: billing part of the quantity leaves the rest
as balance, and every reversal path gives it back. Drives the mark/unmark
helpers against a tiny in-memory stand-in for the service_records row.
Run: python test_service_partial_billing.py"""
from modules.FIN01.model import (_mark_service_record_billed,
                                 _unmark_service_record_billed,
                                 would_overbill)


class FakeCursor:
    """Applies the two helpers' SQL to a dict row — the CASE logic is
    re-expressed here, so this checks the arithmetic and the call sites,
    not Postgres itself."""

    def __init__(self, billable):
        self.row = {'billable_quantity': billable, 'billed_quantity': 0,
                    'is_billed': 0, 'bill_id': None}

    def execute(self, sql, params=None):
        qty = float(params[0])
        r = self.row
        total = float(r['billable_quantity'] or 0)
        if 'GREATEST' in sql:                      # unmark
            r['billed_quantity'] = max(r['billed_quantity'] - qty, 0)
            if r['billed_quantity'] <= 0:
                r['bill_id'] = None
        else:                                      # mark
            r['billed_quantity'] += qty
            r['bill_id'] = params[1]
        r['is_billed'] = 1 if (total > 0 and r['billed_quantity'] >= total) else 0


def test_partial_bill_leaves_balance():
    cur = FakeCursor(100)
    _mark_service_record_billed(cur, 7, 30, bill_id=5)
    assert cur.row['billed_quantity'] == 30
    assert cur.row['is_billed'] == 0        # still billable
    assert cur.row['bill_id'] == 5


def test_billing_the_balance_closes_it():
    cur = FakeCursor(100)
    _mark_service_record_billed(cur, 7, 30, bill_id=5)
    _mark_service_record_billed(cur, 7, 70, bill_id=6)
    assert cur.row['billed_quantity'] == 100
    assert cur.row['is_billed'] == 1


def test_reversal_returns_only_that_bills_share():
    cur = FakeCursor(100)
    _mark_service_record_billed(cur, 7, 30, bill_id=5)
    _mark_service_record_billed(cur, 7, 70, bill_id=6)
    _unmark_service_record_billed(cur, 7, 70)       # bill 6 deleted
    assert cur.row['billed_quantity'] == 30
    assert cur.row['is_billed'] == 0
    _unmark_service_record_billed(cur, 7, 30)       # bill 5 deleted too
    assert cur.row['billed_quantity'] == 0
    assert cur.row['bill_id'] is None


def test_overbill_guard():
    # 30 already billed of 100: 70 is fine, 71 is not.
    assert not would_overbill(30, 70, 100)
    assert would_overbill(30, 71, 100)


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
    print('all passed')
