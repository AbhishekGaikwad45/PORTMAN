"""SAP Invoice_Amount holds its documented contract when the display total
changes underneath it.

SAP_Payload_Guide §4: Invoice_Amount = taxable + GST + TDS - TCS + round-off.
total_amount now includes TCS, so rebuilding the base from total_amount would
double-count it — the builder derives the base from the components instead.
Run: python test_sap_invoice_amount.py"""
from sap_builder import _total_invoice_amount


# Prod BILL/101 shape, carried onto an invoice: 502000 basic, 18% GST, TCS 2%.
LINES = [{'line_amount': 502000.00, 'cgst_amount': 45180.00,
          'sgst_amount': 45180.00, 'igst_amount': 0.0,
          'tds_amount': 0.0, 'tcs_amount': 11847.20}]

HEADER = {'subtotal': 502000.00, 'cgst_amount': 45180.00, 'sgst_amount': 45180.00,
          'igst_amount': 0.0, 'tds_amount': 0.0, 'tcs_amount': 11847.20,
          'round_off': 0.0,
          'total_amount': 604207.20}   # display total, now TCS-inclusive


def test_tcs_is_not_double_counted():
    # 592360.00 + 0 TDS - 11847.20 TCS = 580512.80. Reading total_amount as the
    # base would have produced 592360.00 — TCS counted once in, once out.
    assert round(_total_invoice_amount(HEADER, LINES), 2) == 580512.80


def test_unchanged_for_a_bill_with_no_tcs():
    header = dict(HEADER, tcs_amount=0.0, total_amount=592360.00)
    lines = [dict(LINES[0], tcs_amount=0.0)]
    assert round(_total_invoice_amount(header, lines), 2) == 592360.00


def test_tds_is_added_back():
    header = dict(HEADER, tds_amount=10040.00, tcs_amount=0.0,
                  total_amount=592360.00)
    lines = [dict(LINES[0], tds_amount=10040.00, tcs_amount=0.0)]
    assert round(_total_invoice_amount(header, lines), 2) == 602400.00


def test_prod_payload_dppl_26_27_164():
    """Real posted payload: taxable 1360962.46 + 122486.62 + 122486.62 CGST/SGST,
    TCS 4000.00 -> Invoice_Amount "1601935.70". Anchors the contract to a
    document SAP actually accepted."""
    lines = [{'line_amount': 1360962.46, 'cgst_amount': 122486.62,
              'sgst_amount': 122486.62, 'igst_amount': 0.0,
              'tds_amount': 0.0, 'tcs_amount': 4000.00}]
    header = {'subtotal': 1360962.46, 'cgst_amount': 122486.62,
              'sgst_amount': 122486.62, 'igst_amount': 0.0,
              'tds_amount': 0.0, 'tcs_amount': 4000.00, 'round_off': 0.0,
              'total_amount': 1609935.70}   # display total, TCS-inclusive
    assert round(_total_invoice_amount(header, lines), 2) == 1601935.70


def test_round_off_carries():
    header = dict(HEADER, round_off=0.20)
    assert round(_total_invoice_amount(header, LINES), 2) == 580513.00


def test_falls_back_to_lines_when_header_has_no_components():
    header = {'tds_amount': 0.0, 'tcs_amount': 11847.20, 'round_off': 0.0}
    assert round(_total_invoice_amount(header, LINES), 2) == 580512.80


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
    print('all passed')
