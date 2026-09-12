"""SAP Invoice_Amount is the customer's debit line: it must equal the face
value of the invoice the customer was handed, i.e. the e-invoice TotInvVal.

SAP_Payload_Guide §4: Invoice_Amount = taxable + GST + TCS + round-off.
Three traps this pins down:
  * TCS is collected *from* the customer, so it is ADDED. It was subtracted
    between 2026-05-15 and 2026-09-11, understating every TCS receivable by 2x.
  * TDS does not move the total at all. It was added over the same window,
    overstating every TDS receivable by 1x.
  * total_amount already includes TCS, so rebuilding the base from it would
    double-count — the builder derives the base from the components instead.
Run: python test_sap_invoice_amount.py"""
from sap_builder import _total_invoice_amount


# Prod BILL/101 shape, carried onto an invoice: 502000 basic, 18% GST, TCS 2%.
LINES = [{'line_amount': 502000.00, 'cgst_amount': 45180.00,
          'sgst_amount': 45180.00, 'igst_amount': 0.0,
          'tds_amount': 0.0, 'tcs_amount': 11847.20}]

HEADER = {'subtotal': 502000.00, 'cgst_amount': 45180.00, 'sgst_amount': 45180.00,
          'igst_amount': 0.0, 'tds_amount': 0.0, 'tcs_amount': 11847.20,
          'round_off': 0.0,
          'total_amount': 604207.20}   # display total, TCS-inclusive


def test_tcs_is_added_not_subtracted():
    # 592360.00 + 11847.20 TCS = 604207.20. Subtracting it gave 580512.80 —
    # 2x TCS short of what the items credit, so the document did not balance.
    assert round(_total_invoice_amount(HEADER, LINES), 2) == 604207.20


def test_tcs_is_not_double_counted():
    # Same answer, but it must come from the components: using total_amount as
    # the base and then adding TCS would produce 616054.40.
    assert round(_total_invoice_amount(HEADER, LINES), 2) != 616054.40


def test_unchanged_for_a_bill_with_no_tcs():
    header = dict(HEADER, tcs_amount=0.0, total_amount=592360.00)
    lines = [dict(LINES[0], tcs_amount=0.0)]
    assert round(_total_invoice_amount(header, lines), 2) == 592360.00


def test_tds_does_not_move_the_receivable():
    # The customer is invoiced gross and withholds TDS when they pay, so the
    # receivable is unchanged by it. Adding it gave 602400.00, deducting it
    # 582320.00 — both put SAP out of step with the printed invoice.
    header = dict(HEADER, tds_amount=10040.00, tcs_amount=0.0,
                  total_amount=592360.00)
    lines = [dict(LINES[0], tds_amount=10040.00, tcs_amount=0.0)]
    assert round(_total_invoice_amount(header, lines), 2) == 592360.00


def test_invoice_dppl_26_27_66_with_tds():
    """Real TDS invoice: 200000 taxable + 18000 + 18000 CGST/SGST, TDS 4000.
    Header stores total_amount 236000.00 — gross — and that is what the printed
    invoice and the e-invoice TotInvVal both show, so SAP gets 236000.00."""
    lines = [{'line_amount': 200000.00, 'cgst_amount': 18000.00,
              'sgst_amount': 18000.00, 'igst_amount': 0.0,
              'tds_amount': 4000.00, 'tcs_amount': 0.0}]
    header = {'subtotal': 200000.00, 'cgst_amount': 18000.00,
              'sgst_amount': 18000.00, 'igst_amount': 0.0,
              'tds_amount': 4000.00, 'tcs_amount': 0.0, 'round_off': 0.0,
              'total_amount': 236000.00}
    assert round(_total_invoice_amount(header, lines), 2) == 236000.00


def test_prod_bill_dppl_26_27_265():
    """Scrap sale posted 2026-09-11: 31200 taxable + 2808 + 2808 CGST/SGST,
    TCS 736.32. The bill showed 37552.32; the payload went out as 36079.68.
    This is the document that surfaced the inverted sign."""
    lines = [{'line_amount': 31200.00, 'cgst_amount': 2808.00,
              'sgst_amount': 2808.00, 'igst_amount': 0.0,
              'tds_amount': 0.0, 'tcs_amount': 736.32}]
    header = {'subtotal': 31200.00, 'cgst_amount': 2808.00, 'sgst_amount': 2808.00,
              'igst_amount': 0.0, 'tds_amount': 0.0, 'tcs_amount': 736.32,
              'round_off': 0.0, 'total_amount': 37552.32}
    assert round(_total_invoice_amount(header, lines), 2) == 37552.32


def test_prod_payload_dppl_26_27_164():
    """Taxable 1360962.46 + 122486.62 + 122486.62 CGST/SGST, TCS 4000.00.
    Posted as "1601935.70" under the inverted sign; the correct customer debit
    is 1609935.70, which is also what the bill showed."""
    lines = [{'line_amount': 1360962.46, 'cgst_amount': 122486.62,
              'sgst_amount': 122486.62, 'igst_amount': 0.0,
              'tds_amount': 0.0, 'tcs_amount': 4000.00}]
    header = {'subtotal': 1360962.46, 'cgst_amount': 122486.62,
              'sgst_amount': 122486.62, 'igst_amount': 0.0,
              'tds_amount': 0.0, 'tcs_amount': 4000.00, 'round_off': 0.0,
              'total_amount': 1609935.70}
    assert round(_total_invoice_amount(header, lines), 2) == 1609935.70


def test_round_off_carries():
    header = dict(HEADER, round_off=0.20)
    assert round(_total_invoice_amount(header, LINES), 2) == 604207.40


def test_falls_back_to_lines_when_header_has_no_components():
    header = {'tds_amount': 0.0, 'tcs_amount': 11847.20, 'round_off': 0.0}
    assert round(_total_invoice_amount(header, LINES), 2) == 604207.20


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print('ok', name)
    print('all passed')
