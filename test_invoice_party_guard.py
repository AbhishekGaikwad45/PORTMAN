"""One invoice = one party. Covers the two guards in FINV01.create_invoice:
single_party (no merging customers) and same_party (declaration retro-edited
after billing). Run: python test_invoice_party_guard.py"""
from modules.FIN01.model import same_party, single_party


def test_single_party():
    jsw   = {'customer_type': 'Customer', 'customer_id': 7}
    jsw2  = {'customer_type': 'Customer', 'customer_id': 7}
    amba  = {'customer_type': 'Customer', 'customer_id': 12}
    agent = {'customer_type': 'Agent',    'customer_id': 7}

    assert single_party([]) is True
    assert single_party([jsw]) is True
    assert single_party([jsw, jsw2]) is True
    # the production incident: two customers merged into one invoice
    assert single_party([jsw, amba]) is False
    # same id, different type is still two different parties
    assert single_party([jsw, agent]) is False


def test_same_party():
    assert same_party('JSW Steel Limited', 'JSW Steel Limited')
    # formatting drift must not raise a false alarm
    assert same_party('JSW Steel Limited', '  jsw   steel  LIMITED ')
    # a declaration with no customer stakes no claim
    assert same_party('JSW Steel Limited', '')
    assert same_party('JSW Steel Limited', None)
    # the production incident: MBC retyped to another party after billing
    assert not same_party('JSW Steel Limited', 'AMBA RIVER COKE LIMITED')
    # a blank bill customer must not silently swallow a real cargo owner
    assert not same_party('', 'AMBA RIVER COKE LIMITED')


if __name__ == '__main__':
    test_single_party()
    test_same_party()
    print('invoice party guard: OK')
