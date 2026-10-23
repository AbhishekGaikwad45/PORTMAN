"""SAP Invoice_Amount = taxable + GST + TDS + TCS + round-off, TCS_amount sent negative.

SAP's FIIF059 check (DR):  Invoice_Amount + sum(TCS_amount) == sum(Amount + GST + TDS + RO).
With positive TCS the only passing figure was base - TCS, which posted TCS as a
DEBIT (Finance, 2026-10, EURO CORPORATION: wanted customer Dr 44,699.30 / TCS Cr 876.46).
Sending TCS_amount negative makes base + TCS pass and posts TCS as a credit.
Also pins: total_amount already includes TCS, so the base must come from the
components, never from total_amount.
Run: python test_sap_invoice_amount.py"""
from sap_builder import _total_invoice_amount, _balance_invoice_amount


def _doc(taxable, cgst, sgst, tds=0.0, tcs=0.0, round_off=0.0):
    lines = [{'line_amount': taxable, 'cgst_amount': cgst, 'sgst_amount': sgst,
              'igst_amount': 0.0, 'tds_amount': tds, 'tcs_amount': tcs}]
    header = {'subtotal': taxable, 'cgst_amount': cgst, 'sgst_amount': sgst,
              'igst_amount': 0.0, 'tds_amount': tds, 'tcs_amount': tcs,
              'round_off': round_off,
              # display total, TCS-inclusive — must never be the base
              'total_amount': round(taxable + cgst + sgst + tcs + round_off, 2)}
    return header, lines


def _amt(header, lines):
    return round(_total_invoice_amount(header, lines), 2)


def _abap_dr_balances(rec):
    """FIIF059 DR check: inv_amt + sum(TCS) == sum(Amount+GST+TDS+RO)."""
    f = lambda v: round(float(v or 0) * 100)
    d = f(rec['Invoice_Amount']) + sum(f(i['TCS_amount']) for i in rec['ITEM'])
    c = sum(f(i[k]) for i in rec['ITEM']
            for k in ('Amount', 'CGST_AMT', 'SGST_AMT', 'IGST_AMT', 'TDS_amount', 'Round_off_Value'))
    return d == c


def test_euro_corporation_scrap_tcs():
    # Finance's expected entry: customer Dr 44,699.30, TCS Cr 876.46.
    assert _amt(*_doc(37138.00, 3342.42, 3342.42, tcs=876.46)) == 44699.30
    item = {'Amount': '37138.00', 'CGST_AMT': '3342.42', 'SGST_AMT': '3342.42',
            'IGST_AMT': '', 'TDS_amount': '', 'TCS_amount': '-876.46', 'Round_off_Value': ''}
    rec = _balance_invoice_amount({'Invoice_Amount': '44699.30', 'ITEM': [item]})
    assert rec['Invoice_Amount'] == '44699.30'
    assert _abap_dr_balances(rec)


def test_items_carry_negative_tcs():
    from sap_builder import _build_items
    line = {'line_amount': 37138.00, 'cgst_amount': 3342.42, 'sgst_amount': 3342.42,
            'tcs_amount': 876.46, 'tcs_applicable': 1}
    items = _build_items([line], 'X', config_defaults={'tcs_gl': '1404040394'}, svc_map={})
    assert items[0]['TCS_amount'] == '-876.46'
    assert items[0]['TCS_GL'] == '1404040394'


def test_tds_is_added():
    # DPPL/26-27/2: 236000 rejected (log 31), 232000 rejected (log 35),
    # 240000 accepted (log 36). Same for /66 (log 153).
    assert _amt(*_doc(200000, 18000, 18000, tds=4000)) == 240000.00


def test_no_tds_no_tcs_is_the_base():
    assert _amt(*_doc(502000, 45180, 45180)) == 592360.00


def test_round_off_carries():
    # DPPL/26-27/38: 476003.00 accepted (log 136) — two lines, TDS 4000, RO +3.
    h, l = _doc(400000, 36000, 36000, tds=4000, round_off=3.0)
    assert _amt(h, l) == 476003.00


def test_falls_back_to_lines_when_header_has_no_components():
    _, lines = _doc(200000, 18000, 18000, tcs=4000)
    assert _amt({'tcs_amount': 0.0, 'round_off': 0.0}, lines) == 240000.00


def test_sent_amount_balances_items_even_if_header_is_stale():
    # Header says CGST 90.01, line says 90.00: the items carry 90.00, so SAP's
    # check only passes if Invoice_Amount is derived from the items.
    item = {'Amount': '1000.00', 'CGST_AMT': '90.00', 'SGST_AMT': '90.00',
            'IGST_AMT': '', 'TDS_amount': '4.00', 'TCS_amount': '-11.80',
            'Round_off_Value': '-0.20'}
    rec = _balance_invoice_amount({'Invoice_Amount': '1195.61', 'ITEM': [item]})
    assert rec['Invoice_Amount'] == '1195.60'   # 1000+90+90+4-0.20+11.80
    assert _abap_dr_balances(rec)


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
    print('all passed')
