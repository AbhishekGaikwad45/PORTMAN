"""
Unit tests for the go-live cutover logic. Pure helpers are tested without a DB
(matching the test_sap_builder.py pattern); DB-touching paths are covered by
the lock-gate tests via monkeypatch.
"""
from modules.FIN01 import model


# --- Task 2: next_from_seed (floor) ----------------------------------------

def test_next_from_seed_no_existing_uses_seed():
    assert model.next_from_seed(0, 4568) == 4568


def test_next_from_seed_existing_at_seed_increments():
    assert model.next_from_seed(4568, 4568) == 4569


def test_next_from_seed_existing_above_seed_dominates():
    assert model.next_from_seed(5000, 4568) == 5001


def test_next_from_seed_no_seed_is_plain_increment():
    assert model.next_from_seed(10, None) == 11
    assert model.next_from_seed(0, None) == 1


# --- Task 4: pure cutover helpers ------------------------------------------
from modules.ADMIN import cutover


def test_validate_start_seq_ok_above_max():
    assert cutover.validate_start_seq(4568, 0) == (True, '')


def test_validate_start_seq_rejects_at_or_below_max():
    ok, msg = cutover.validate_start_seq(5, 10)
    assert ok is False and '10' in msg


def test_validate_start_seq_rejects_non_positive():
    assert cutover.validate_start_seq(0, 0)[0] is False
    assert cutover.validate_start_seq(-3, 0)[0] is False


def test_cargo_source_maps_known_types():
    assert cutover.cargo_source('VCN_IMPORT') == ('vcn_cargo_declaration', 'bl_quantity')
    assert cutover.cargo_source('VCN_EXPORT') == ('vcn_export_cargo_declaration', 'bl_quantity')
    assert cutover.cargo_source('MBC') == ('mbc_customer_details', 'quantity')


def test_cargo_source_unknown_is_none():
    assert cutover.cargo_source('NOPE') is None


# --- Task 6: mark/unmark dispatch (no DB, fake cursor) ----------------------

class _FakeCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1
        # fetchone() returns a dict representing a row with total=100, already=0
        # so that compute_partial_billed gets sensible inputs in the billed=True path.
        self._fetchone_row = {'total': 100, 'already': 0}

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self._fetchone_row


def test_apply_billed_marks_cargo_and_services():
    cur = _FakeCursor()
    counts = cutover._apply_billed(
        cur,
        [{'source_type': 'VCN_IMPORT', 'id': 7}],
        [42],
        billed=True,
    )
    assert counts == {'cargo': 1, 'services': 1}
    sqls = [c[0] for c in cur.calls]
    # New behaviour: SELECT totals first, then UPDATE with explicit param values.
    assert any("SELECT bl_quantity AS total, COALESCE(billed_quantity, 0) AS already FROM vcn_cargo_declaration WHERE id=%s" in s for s in sqls)
    assert any("UPDATE vcn_cargo_declaration SET is_billed=%s, billed_quantity=%s WHERE id=%s" in s for s in sqls)
    assert any("UPDATE service_records SET is_billed=1 WHERE id=%s" in s for s in sqls)
    # The computed values must thread through: total=100, already=0, no bill_quantity
    # -> compute_partial_billed(100, 0, None) -> (100.0, 1); UPDATE params [is_billed, new_billed, id].
    update_call = next(c for c in cur.calls if "UPDATE vcn_cargo_declaration" in c[0])
    assert update_call[1] == [1, 100.0, 7]


def test_apply_billed_skips_missing_row():
    cur = _FakeCursor()
    cur._fetchone_row = None  # row not found
    counts = cutover._apply_billed(cur, [{'source_type': 'VCN_IMPORT', 'id': 9}], [], billed=True)
    assert counts == {'cargo': 0, 'services': 0}
    sqls = [c[0] for c in cur.calls]
    # SELECT happens, but no UPDATE on the declaration table for a missing row.
    assert any("SELECT bl_quantity AS total" in s for s in sqls)
    assert not any("UPDATE vcn_cargo_declaration" in s for s in sqls)


def test_apply_billed_unknown_source_raises():
    import pytest
    cur = _FakeCursor()
    with pytest.raises(ValueError):
        cutover._apply_billed(cur, [{'source_type': 'XXX', 'id': 1}], [], billed=True)


# --- Task 9: lock gate (monkeypatched, no DB) ------------------------------

def test_set_invoice_seed_blocked_when_locked(monkeypatch):
    monkeypatch.setattr(cutover, 'is_locked', lambda: True)
    ok, msg = cutover.set_invoice_seed('DPPL', '26-27', 4568, 'tester')
    assert ok is False and 'locked' in msg.lower()


def test_set_fdcn_seed_blocked_when_locked(monkeypatch):
    monkeypatch.setattr(cutover, 'is_locked', lambda: True)
    ok, msg = cutover.set_fdcn_seed('DPPLCN', '26-27', 4568, 'tester')
    assert ok is False and 'locked' in msg.lower()


def test_mark_items_billed_blocked_when_locked(monkeypatch):
    monkeypatch.setattr(cutover, 'is_locked', lambda: True)
    ok, msg, counts = cutover.mark_items_billed([{'source_type': 'VCN_IMPORT', 'id': 1}], [], 'tester')
    assert ok is False and 'locked' in msg.lower() and counts == {}


# --- Partial cutover billing math ------------------------------------------

def test_compute_partial_billed_partial_below_total():
    # 50 of 100, nothing billed yet -> stays open (is_billed=0)
    assert cutover.compute_partial_billed(100, 0, 50) == (50.0, 0)


def test_compute_partial_billed_accumulates_onto_existing():
    # 20 already billed + 30 now = 50 of 100 -> still open
    assert cutover.compute_partial_billed(100, 20, 30) == (50.0, 0)


def test_compute_partial_billed_reaches_total_sets_flag():
    # 50 already + 50 now = 100 of 100 -> fully billed
    assert cutover.compute_partial_billed(100, 50, 50) == (100.0, 1)


def test_compute_partial_billed_caps_over_balance():
    # only 20 left, asking for 50 -> capped at 20, fully billed
    assert cutover.compute_partial_billed(100, 80, 50) == (100.0, 1)


def test_compute_partial_billed_defaults_to_full_balance_when_missing():
    # bill_qty None or 0 -> mark the whole remaining balance (back-compat)
    assert cutover.compute_partial_billed(100, 30, None) == (100.0, 1)
    assert cutover.compute_partial_billed(100, 30, 0) == (100.0, 1)


def test_compute_partial_billed_rounds_to_three_decimals():
    assert cutover.compute_partial_billed(10, 0, 3.3335) == (3.334, 0)


def test_compute_partial_billed_negative_qty_defaults_to_balance():
    # negative bill_qty is treated like None/0 -> mark the whole remaining balance
    assert cutover.compute_partial_billed(100, 30, -5) == (100.0, 1)


def test_compute_partial_billed_stale_already_over_total_never_negative():
    # legacy inconsistency: already billed > total -> balance clamps to 0,
    # nothing more is billed and the line reads as fully billed
    assert cutover.compute_partial_billed(100, 120, 10) == (120.0, 1)


def test_compute_partial_billed_high_precision_total_still_flags():
    # bl_quantity with >3 decimals: a full-balance mark must still flip is_billed=1
    assert cutover.compute_partial_billed(100.0001, 0, 100.0001) == (100.0, 1)


# --- Reopen: unmark a cutover-flagged MBC line ------------------------------

def test_apply_billed_unmark_resets_cargo_fully():
    """Reopen must reset to zero, not decrement.

    A bad cutover run can leave billed_quantity above the line quantity (prod had
    2539.00 flagged against a 254.00 MT MBC line). Subtracting would leave the row
    still flagged; a full reset is what makes it billable again.
    """
    cur = _FakeCursor()
    counts = cutover._apply_billed(
        cur,
        [{'source_type': 'MBC', 'id': 31}],
        [],
        billed=False,
    )
    assert counts == {'cargo': 1, 'services': 0}
    sqls = [c[0] for c in cur.calls]
    # No SELECT needed on the unmark path — it is an unconditional reset.
    assert not any('SELECT' in s for s in sqls)
    reset = next(c for c in cur.calls if 'UPDATE mbc_customer_details' in c[0])
    assert 'SET is_billed=0, billed_quantity=0 WHERE id=%s' in reset[0]
    assert reset[1] == [31]
