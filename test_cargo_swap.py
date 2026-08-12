"""Swap Bill Cargo: replace wrong cargo on a bill without moving the money.

The invariant under test is exactness — the split must sum to the original to
the paisa, because the swap is allowed to run on an already-invoiced bill.
Run: python test_cargo_swap.py"""
from modules.FIN01.model import split_line_amounts, quantities_balance, _MONEY_FIELDS


# The production case: 7816 MT of Illawara Coal billed to the wrong party,
# replaced by four JSW MBC lines that add up to exactly the same quantity.
INCIDENT_QUANTITIES = [3865.0, 275.0, 3590.0, 86.0]


def _line(qty, amount, cgst=0.0, sgst=0.0, igst=0.0):
    return {'quantity': qty, 'line_amount': amount,
            'cgst_amount': cgst, 'sgst_amount': sgst, 'igst_amount': igst,
            'line_total': round(amount + cgst + sgst + igst, 2),
            'tds_amount': 0, 'tcs_amount': 0}


def _sums_match(original, shares):
    for field in _MONEY_FIELDS:
        target = round(float(original.get(field) or 0), 2)
        got = round(sum(s[field] for s in shares), 2)
        assert got == target, f'{field}: {got} != {target}'


def test_quantities_balance():
    assert quantities_balance(7816.0, INCIDENT_QUANTITIES)
    assert not quantities_balance(7816.0, [3865.0, 275.0, 3590.0])
    # float noise is absorbed, a real difference is not
    assert quantities_balance(7816.0, [7815.9995])
    assert not quantities_balance(7816.0, [7815.0])


def test_incident_split_is_exact():
    # A rate that does not divide cleanly across four shares.
    original = _line(7816.0, 1234567.89, cgst=111111.11, sgst=111111.11)
    shares = split_line_amounts(original, INCIDENT_QUANTITIES)
    assert len(shares) == 4
    _sums_match(original, shares)


def test_residual_lands_on_the_largest_share():
    # 100.00 over three equal parts is 33.33 x3 = 99.99; the odd paisa must go
    # somewhere deterministic rather than vanish.
    original = _line(3.0, 100.00)
    shares = split_line_amounts(original, [1.0, 1.0, 1.0])
    _sums_match(original, shares)
    assert sorted(s['line_amount'] for s in shares) == [33.33, 33.33, 33.34]

    original = _line(6.0, 100.00)
    shares = split_line_amounts(original, [1.0, 4.0, 1.0])
    _sums_match(original, shares)
    assert shares[1]['line_amount'] == max(s['line_amount'] for s in shares)


def test_single_replacement_is_a_straight_move():
    original = _line(7816.0, 999999.99, igst=179999.99)
    shares = split_line_amounts(original, [7816.0])
    assert len(shares) == 1
    _sums_match(original, shares)
    assert shares[0]['line_amount'] == 999999.99
    assert shares[0]['igst_amount'] == 179999.99


def test_awkward_amounts_never_drift():
    # Brute force: many splits, every one must reconcile to the paisa.
    for amount in (0.01, 0.03, 1.00, 33.33, 7816.55, 1234567.89):
        for quantities in ([1.0, 1.0, 1.0], [3865.0, 275.0, 3590.0, 86.0],
                           [7.0, 11.0, 13.0], [0.001, 999.999]):
            original = _line(sum(quantities), amount,
                             cgst=round(amount * 0.09, 2), sgst=round(amount * 0.09, 2))
            _sums_match(original, split_line_amounts(original, quantities))


def test_zero_and_empty_are_refused():
    for bad in ([], [0.0], [0.0, 0.0]):
        try:
            split_line_amounts(_line(10.0, 100.0), bad)
        except ValueError:
            continue
        raise AssertionError(f'expected ValueError for {bad}')


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
    print('cargo swap: OK')
