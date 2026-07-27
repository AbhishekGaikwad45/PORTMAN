"""A berth stays occupied until cast off, not until balance hits zero — no DB."""
import json
import modules.RP01.RP01.port_overview.views as pov

LAYOUT = [
    {'berth': 'BERTH 8',  'name': 'JSW SURYAGAD', 'type': 'MBC',   'position': 'A/S'},
    {'berth': 'BERTH 9',  'name': 'FALCON HIGH',  'type': 'BARGE', 'position': 'A/S'},
    {'berth': 'BERTH 10', 'name': 'JSW LOHGAD',   'type': 'MBC',   'position': 'A/S'},
]

# Berth 9 finished discharging (balance 0) but has not cast off, so
# _fetch_all_barges() still returns it. Berth 1's barge is gone from that list.
LIVE = [
    {'name': 'JSW SURYAGAD', 'cargo': 'BRBF Fines',      'total_qty': 7682, 'balance_qty': 3300,
     'unloading_commenced': '2026-07-26 08:00'},
    {'name': 'FALCON HIGH',  'cargo': 'Dhamra Fines-MIX', 'total_qty': 1400, 'balance_qty': 0,
     'commence_discharge_berth': '2026-07-26 14:00'},
    {'name': 'JSW LOHGAD',   'cargo': 'Vizag Fines',      'total_qty': 7465, 'balance_qty': 7465,
     'unloading_commenced': ''},
]


def _occupancy(layout, live, monkeypatch):
    class _Cur:
        def execute(self, *a, **k): pass
        def fetchone(self): return {'berth_layout': json.dumps(layout)}

    monkeypatch.setattr(pov, 'get_db', lambda: type('C', (), {'close': lambda s: None})())
    monkeypatch.setattr(pov, 'get_cursor', lambda conn: _Cur())
    monkeypatch.setattr(pov, '_fetch_all_barges', lambda: (live, set()))
    return pov._fetch_berth_occupancy()


def test_zero_balance_still_holds_the_berth(monkeypatch):
    occ = _occupancy(LAYOUT, LIVE, monkeypatch)
    # The regression: berth 9 vanished from the map because its balance was 0.
    assert 'BERTH 9' in occ
    assert occ['BERTH 9'][0]['name'] == 'FALCON HIGH'
    assert occ['BERTH 9'][0]['balance_qty'] == 0
    # Statuses use the Barge Position vocabulary so both screens colour alike:
    # amber = discharged but still moored, green = working, cyan = waiting.
    assert occ['BERTH 9'][0]['status'] == 'Discharge Completed'
    assert occ['BERTH 8'][0]['status'] == 'Under Discharge'
    assert occ['BERTH 10'][0]['status'] == 'Waiting'  # alongside, not commenced


def test_cast_off_frees_the_berth(monkeypatch):
    # FALCON HIGH cast off -> _fetch_all_barges() drops it -> berth clears.
    live = [b for b in LIVE if b['name'] != 'FALCON HIGH']
    occ = _occupancy(LAYOUT, live, monkeypatch)
    assert 'BERTH 9' not in occ
    assert 'BERTH 8' in occ and 'BERTH 10' in occ
