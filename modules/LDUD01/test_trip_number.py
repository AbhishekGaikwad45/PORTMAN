"""Self-check for the barge trip_number recalculation rule (no DB needed)."""
from modules.LDUD01.model import _needs_new_trip


def test_needs_new_trip():
    # No barge -> never renumber
    assert not _needs_new_trip('', '', None)
    assert not _needs_new_trip('MSC ANNA', '', 3)

    # Placeholder row getting its first barge
    assert _needs_new_trip('', 'MSC Anna', 1)

    # Barge actually changed
    assert _needs_new_trip('MSC Anna', 'Ever Given', 2)

    # Row has no number yet
    assert _needs_new_trip('MSC Anna', 'MSC Anna', None)

    # Unchanged row keeps its number — case and whitespace insensitive
    assert not _needs_new_trip('MSC Anna', 'MSC Anna', 2)
    assert not _needs_new_trip('msc anna ', ' MSC ANNA', 2)
