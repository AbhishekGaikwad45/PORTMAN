"""SAP Invoice_Amount = taxable + GST + TDS - TCS + round-off.

This is what SAP's posting balances against, proven by SAP's own responses in
integration_logs (every case below is a real payload + real SAP verdict).
It is NOT the printed face value — the 2026-09-11 attempt to make it the face
value (+TCS, TDS excluded) got "Total Debit and Credit amount is not matching"
on every TDS/TCS invoice (e.g. DPPL/26-27/316).
Also pins: total_amount already includes TCS, so the base must come from the
components, never from total_amount.
Run: python test_sap_invoice_amount.py"""
from sap_builder import _total_invoice_amount


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


def test_tcs_is_subtracted():
    # DPPL/26-27/7: 240000 (+TCS) rejected (log 42), 232000 (-TCS) accepted (log 43).
    assert _amt(*_doc(200000, 18000, 18000, tcs=4000)) == 232000.00


def test_tds_is_added():
    # DPPL/26-27/2: 236000 rejected (log 31), 232000 rejected (log 35),
    # 240000 accepted (log 36). Same for /66 (log 153).
    assert _amt(*_doc(200000, 18000, 18000, tds=4000)) == 240000.00


def test_prod_dppl_26_27_316_scrap_tcs():
    # Rejected 2026-10-12 at 91606.00 (+TCS). SAP-balanced figure is base - TCS.
    assert _amt(*_doc(76110.00, 6849.90, 6849.90, tcs=1796.20)) == 88013.60


def test_prod_dppl_26_27_109_accepted():
    # Accepted (log 177) at 1601935.70.
    assert _amt(*_doc(1360962.46, 122486.62, 122486.62, tcs=4000)) == 1601935.70


def test_no_tds_no_tcs_is_the_base():
    assert _amt(*_doc(502000, 45180, 45180)) == 592360.00


def test_round_off_carries():
    # DPPL/26-27/38: 476003.00 accepted (log 136) — two lines, TDS 4000, RO +3.
    h, l = _doc(400000, 36000, 36000, tds=4000, round_off=3.0)
    assert _amt(h, l) == 476003.00


def test_falls_back_to_lines_when_header_has_no_components():
    _, lines = _doc(200000, 18000, 18000, tcs=4000)
    assert _amt({'tcs_amount': 0.0, 'round_off': 0.0}, lines) == 232000.00


def test_sent_amount_balances_items_even_if_header_is_stale():
    # Header says CGST 90.01, line says 90.00: the items carry 90.00, so SAP's
    # check only passes if Invoice_Amount is derived from the items.
    from sap_builder import _balance_invoice_amount
    item = {'Amount': '1000.00', 'CGST_AMT': '90.00', 'SGST_AMT': '90.00',
            'IGST_AMT': '', 'TDS_amount': '4.00', 'TCS_amount': '11.80',
            'Round_off_Value': '-0.20'}
    rec = _balance_invoice_amount({'Invoice_Amount': '1168.21', 'ITEM': [item]})
    assert rec['Invoice_Amount'] == '1172.00'   # 1000+90+90+4-0.20-11.80


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
    print('all passed')
