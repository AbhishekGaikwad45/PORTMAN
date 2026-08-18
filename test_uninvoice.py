"""Pure checks for the admin Uninvoice / Remove-Bill tabs and the aggregate
GST reconciliation — no DB."""
from modules.FIN01.model import (
    _can_uninvoice, _can_revert_to_draft, would_overbill, compute_aggregate_gst,
    _amount_in_words,
)


def test_can_uninvoice():
    # Missing invoice / real SAP doc / already cancelled → refused
    assert _can_uninvoice(None)[0] is False
    assert _can_uninvoice({'sap_document_number': '5100001234',
                           'invoice_status': 'Posted to GST'})[0] is False
    assert _can_uninvoice({'sap_document_number': '',
                           'invoice_status': 'Cancelled'})[0] is False
    # No SAP doc (null / empty / whitespace-only) and not cancelled → allowed
    assert _can_uninvoice({'sap_document_number': None,
                           'invoice_status': 'Queued for SAP'})[0] is True
    assert _can_uninvoice({'sap_document_number': '',
                           'invoice_status': 'Posted to SAP'})[0] is True
    assert _can_uninvoice({'sap_document_number': '   ',
                           'invoice_status': 'Generated'})[0] is True


def test_can_revert_to_draft():
    # Only an approved bill goes back to Draft; everything else is refused
    assert _can_revert_to_draft({'bill_status': 'Approved'})[0] is True
    for st in ('Draft', 'Pending Approval', 'Rejected', 'Invoiced', 'Cancelled'):
        assert _can_revert_to_draft({'bill_status': st})[0] is False
    assert _can_revert_to_draft(None)[0] is False


def test_would_overbill():
    # Exact duplicate: source already fully billed, second full bill → blocked
    assert would_overbill(100, 100, 100) is True
    # First full bill on an unbilled source → allowed
    assert would_overbill(0, 100, 100) is False
    # Legitimate partial billing up to the full quantity → allowed
    assert would_overbill(50, 50, 100) is False
    assert would_overbill(0, 50, 100) is False
    # Over the remaining balance → blocked
    assert would_overbill(60, 50, 100) is True
    # Float noise within tolerance on a full bill → allowed
    assert would_overbill(0, 100.0000001, 100) is False
    # None-safe
    assert would_overbill(None, None, None) is False


def test_aggregate_gst_matches_sap():
    # The real DPPL/26-27/175 case: one line, 9% + 9% on 9,951,547.32.
    # Per-line rounding gave 895639.25; SAP computes round(base*9%)=895639.26.
    line_gst, totals = compute_aggregate_gst(
        [{'id': 1, 'line_amount': 9951547.32,
          'cgst_rate': 9, 'sgst_rate': 9, 'igst_rate': 0}])
    assert totals['cgst_amount'] == 895639.26, totals
    assert totals['sgst_amount'] == 895639.26, totals
    assert totals['igst_amount'] == 0.0
    grand = totals['subtotal'] + totals['cgst_amount'] + totals['sgst_amount']
    assert round(grand, 2) == 11742825.84, grand
    assert line_gst[1]['line_total'] == round(9951547.32 + 895639.26 + 895639.26, 2)


def test_aggregate_gst_residual_distributed():
    # Two lines at 9%+9% whose per-line rounding drifts; group total must equal
    # round(sum*rate) and the per-line amounts must sum back to it exactly.
    lines = [{'id': 1, 'line_amount': 100.005, 'cgst_rate': 9, 'sgst_rate': 9, 'igst_rate': 0},
             {'id': 2, 'line_amount': 100.005, 'cgst_rate': 9, 'sgst_rate': 9, 'igst_rate': 0}]
    line_gst, totals = compute_aggregate_gst(lines)
    taxable = 200.01
    assert totals['cgst_amount'] == round(taxable * 0.09, 2)
    assert round(line_gst[1]['cgst_amount'] + line_gst[2]['cgst_amount'], 2) == totals['cgst_amount']


def test_aggregate_gst_igst_and_zero():
    # IGST line + a zero-rated line: IGST on aggregate, zero stays zero.
    lines = [{'id': 1, 'line_amount': 1000.0, 'cgst_rate': 0, 'sgst_rate': 0, 'igst_rate': 18},
             {'id': 2, 'line_amount': 500.0, 'cgst_rate': 0, 'sgst_rate': 0, 'igst_rate': 0}]
    line_gst, totals = compute_aggregate_gst(lines)
    assert totals['igst_amount'] == 180.0
    assert line_gst[2]['igst_amount'] == 0.0


def test_amount_in_words():
    assert _amount_in_words(11742825.84) == (
        'Rupees One Crore Seventeen Lakh Forty Two Thousand Eight Hundred '
        'Twenty Five and Eighty Four Paise Only')
    assert _amount_in_words(500.00) == 'Rupees Five Hundred Only'
    assert _amount_in_words(1234.50) == (
        'Rupees One Thousand Two Hundred Thirty Four and Fifty Paise Only')


if __name__ == '__main__':
    test_can_uninvoice()
    test_would_overbill()
    test_aggregate_gst_matches_sap()
    test_aggregate_gst_residual_distributed()
    test_aggregate_gst_igst_and_zero()
    test_amount_in_words()
    print('ok')
