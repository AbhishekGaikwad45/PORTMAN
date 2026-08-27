"""Bill header totals carry the TDS/TCS its lines computed.

Reproduces prod BILL/101: one 502000.00 line at 18% GST with TCS 2% — the TCS
was computed on the line and then dropped, because bill_totals never summed it.
Run: python test_bill_tds_tcs_totals.py"""
from modules.FIN01.model import bill_totals


# The line as save_bill_line stores it: GST on the basic, TCS on basic + GST.
LINE_101 = {'line_amount': 502000.00, 'cgst_amount': 45180.00,
            'sgst_amount': 45180.00, 'igst_amount': 0.0,
            'tds_amount': 0.0, 'tcs_amount': 11847.20}


def test_tcs_reaches_the_header():
    t = bill_totals([LINE_101])
    assert t['subtotal'] == 502000.00
    assert t['cgst_amount'] == 45180.00
    assert t['sgst_amount'] == 45180.00
    assert t['tcs_amount'] == 11847.20      # was silently 0 / absent


def test_tcs_is_two_percent_of_basic_plus_gst():
    # 2% of (502000 + 90360) = 11847.20 — what save_bill_line computes.
    assert round((502000.00 + 45180.00 + 45180.00) * 2 / 100, 2) == 11847.20


def test_total_amount_includes_tcs():
    # TCS is collected from the customer, so it is part of what they owe.
    assert bill_totals([LINE_101])['total_amount'] == 604207.20


def test_tds_sums_too():
    line = dict(LINE_101, tds_amount=10040.00, tcs_amount=0.0)
    t = bill_totals([line])
    assert t['tds_amount'] == 10040.00
    assert t['total_amount'] == 592360.00    # TDS never reduces the bill


def test_lines_without_tax_flags_are_zero():
    t = bill_totals([{'line_amount': 100, 'cgst_amount': 9, 'sgst_amount': 9}])
    assert t['tds_amount'] == 0 and t['tcs_amount'] == 0


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
    print('all passed')
