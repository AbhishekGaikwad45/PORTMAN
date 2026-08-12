"""
FDCN01 doc-number prefix resolution. The DN/CN series is configurable again
(CNDS01); with nothing configured the built-in prefixes still apply, so an
existing series keeps numbering exactly as before until someone sets a default.
"""
from modules.FDCN01.model import normalize_prefix, resolve_doc_prefix


def test_falls_back_when_nothing_configured():
    assert resolve_doc_prefix('', 'CN') == 'DPPLCN'
    assert resolve_doc_prefix(None, 'DN') == 'DPPLDN'
    assert resolve_doc_prefix('   ', 'CN') == 'DPPLCN'


def test_unknown_type_falls_back_to_doc_type():
    assert resolve_doc_prefix('', 'XX') == 'XX'


def test_configured_series_wins():
    assert resolve_doc_prefix('DPPLCN2', 'CN') == 'DPPLCN2'


def test_prefix_is_normalized():
    # The FY and sequence are appended downstream, so outer slashes must go —
    # the legacy rows in the database are stored as 'DPPL/CN/26-27/'.
    assert normalize_prefix('DPPL/CN/26-27/') == 'DPPL/CN/26-27'
    assert normalize_prefix('  dpplcn  ') == 'DPPLCN'
    assert normalize_prefix('/DPPLCN/') == 'DPPLCN'
    assert normalize_prefix('') == ''
    assert normalize_prefix(None) == ''
    # inner separators are the user's own convention and stay untouched
    assert resolve_doc_prefix('DPPL/CN/', 'CN') == 'DPPL/CN'


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
    print('fdcn doc number: OK')
