"""Guard check for the admin Uninvoice tab — the pure decision, no DB."""
from modules.FIN01.model import _can_uninvoice


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
    print('ok')


if __name__ == '__main__':
    test_can_uninvoice()
