"""Pure guard checks for the admin Uninvoice / Remove-Bill tabs — no DB."""
from modules.FIN01.model import _can_uninvoice, would_overbill


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


if __name__ == '__main__':
    test_can_uninvoice()
    test_would_overbill()
    print('ok')
