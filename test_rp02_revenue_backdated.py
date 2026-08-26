"""Pure-function tests for the RP02 backdated revenue upload (no DB).

Two contracts: the template the exporter hands out survives a round trip
through the parser, and every cell is stored as text exactly as the sheet
spells it — a backdated register carries YES/NO in numeric-looking columns,
commas in amounts and notes in date cells, and none of that may fail an upload.
"""
import io
from datetime import date

from modules.RP02 import revenue


class _Upload:
    """Minimal stand-in for a werkzeug FileStorage."""
    def __init__(self, text):
        self._raw = text.encode('utf-8')

    def read(self):
        return self._raw


def _parse(text):
    return revenue.parse_upload(_Upload(text))


# ── template round trip ──────────────────────────────────────────────────────
def test_template_parses_back_with_no_errors():
    rows, errors = _parse(revenue.build_template_csv())
    assert errors == []
    assert len(rows) == len(revenue.TEMPLATE_EXAMPLE_ROWS)


def test_template_maps_every_column():
    rows, _ = _parse(revenue.build_template_csv())
    r = rows[0]
    assert r['invoice_no'] == 'DPPL/25-26/001'
    assert r['invoice_date'] == '2025-04-05'      # normalised from 05-04-2025
    assert r['customer_name'] == 'JSW Steel Ltd'
    assert r['grouping_label'] == 'Cargo Handling Charges'
    assert r['cargo_volume'] == 'YES'             # a flag in the sheet
    assert r['basic_value'] == '2340000.00'       # text, exactly as written
    assert r['booking_status'] == 'Booked'


def test_headers_cover_every_stored_column():
    assert revenue.TEMPLATE_HEADERS[1:-2] == [h for _, h in revenue.FIELDS]
    assert revenue.TEMPLATE_HEADERS[-2:] == ['Days', 'Bucket']
    assert set(revenue.HEADER_MAP.values()) >= set(revenue.COLUMNS)


def test_days_and_bucket_columns_are_ignored_on_upload():
    csv = ('Invoice No.,Date,Customer Name,Basic Value (Rs.),Days,Bucket\n'
           'INV1,05-04-2025,ACME,100,999,> 3 years\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert 'days' not in rows[0] and 'bucket' not in rows[0]


# ── everything is text ───────────────────────────────────────────────────────
def test_amounts_are_stored_verbatim_commas_and_all():
    csv = ('Invoice No.,Date,Customer Name,Basic Value (Rs.),Qty - MT\n'
           'INV1,05-04-2025,ACME,"1,000.00",19500\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert rows[0]['basic_value'] == '1,000.00'
    assert rows[0]['qty'] == '19500'


def test_non_numeric_junk_in_numeric_columns_is_accepted():
    csv = ('Invoice No.,Date,Customer Name,Basic Value (Rs.),TDS/TCS,Cargo Volume\n'
           'INV1,05-04-2025,ACME,not-a-number,N/A,YES\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert rows[0]['basic_value'] == 'not-a-number'
    assert rows[0]['tds_tcs'] == 'N/A'
    assert rows[0]['cargo_volume'] == 'YES'


def test_blank_cells_become_none():
    csv = ('Invoice No.,Date,Customer Name,Basic Value (Rs.)\n'
           'INV1,05-04-2025,ACME,\n')
    rows, _ = _parse(csv)
    assert rows[0]['basic_value'] is None


# ── header handling ──────────────────────────────────────────────────────────
def test_duplicate_revenue_type_headers_fill_1_then_2():
    csv = ('Invoice No.,Date,Customer Name,Revenue type,Revenue type\n'
           'INV1,05-04-2025,ACME,Cargo,Wharfage\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert rows[0]['revenue_type_1'] == 'Cargo'
    assert rows[0]['revenue_type_2'] == 'Wharfage'


def test_title_rows_above_the_header_are_skipped():
    csv = ('Revenue Register FY 2025-26,,,,\n'
           ',,,,\n'
           'Invoice No.,Date,Customer Name,GL CODE,Basic Value (Rs.)\n'
           'INV1,05-04-2025,ACME,4101076010,100\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert rows[0]['gl_code'] == '4101076010'


def test_unrecognisable_file_reports_a_missing_header_row():
    rows, errors = _parse('just,some,junk\n1,2,3\n')
    assert rows == []
    assert 'header row' in errors[0]['message']


# ── the only validation left ─────────────────────────────────────────────────
def test_invoice_no_is_the_one_required_column():
    csv = ('Invoice No.,Date,Customer Name,Basic Value (Rs.)\n'
           ',05-04-2025,ACME,100\n'
           'INV2,,,100\n')
    rows, errors = _parse(csv)
    assert [e['row'] for e in errors] == [2]          # row 2 has no invoice no.
    assert errors[0]['message'] == 'Invoice No. is required'
    assert len(rows) == 1                              # blank date + customer is fine
    assert rows[0]['invoice_date'] is None
    assert rows[0]['customer_name'] is None


def test_blank_rows_are_skipped():
    csv = ('Invoice No.,Date,Customer Name,GL CODE,Basic Value (Rs.)\n'
           ',,,,\n'
           'INV1,05-04-2025,ACME,4101076010,100\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert len(rows) == 1


# ── dates ────────────────────────────────────────────────────────────────────
def test_common_date_formats_all_normalise_to_iso():
    for text in ('2025-04-05', '05-04-2025', '05/04/2025', '05.04.2025', '05-Apr-2025'):
        csv = 'Invoice No.,Date,Customer Name\nINV1,%s,ACME\n' % text
        rows, _ = _parse(csv)
        assert rows[0]['invoice_date'] == '2025-04-05', text


def test_an_unparseable_date_is_kept_as_written():
    csv = ('Invoice No.,Date,Customer Name\n'
           'INV1,Under Discharge,ACME\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert rows[0]['invoice_date'] == 'Under Discharge'


def test_parse_date_never_raises():
    assert revenue.parse_date('') is None
    assert revenue.parse_date(None) is None
    assert revenue.parse_date('tomorrow') is None
    assert revenue.parse_date('05-04-2025') == date(2025, 4, 5)


# ── month key (what an upload replaces) ──────────────────────────────────────
def test_month_key_is_the_year_and_month():
    assert revenue.month_key('2025-04-05') == '2025-04'


def test_month_key_of_an_odd_date_is_stable():
    # Not a real month, but the same file re-uploaded targets the same rows.
    assert revenue.month_key('Under Discharge') == 'Under D'
    assert revenue.month_key(None) == ''


# ── ageing ───────────────────────────────────────────────────────────────────
def test_age_buckets_at_the_boundaries():
    assert revenue.age_bucket(0) == '0-30 days'
    assert revenue.age_bucket(30) == '0-30 days'
    assert revenue.age_bucket(31) == '31-60 days'
    assert revenue.age_bucket(1095) == '2-3 years'
    assert revenue.age_bucket(1096) == '> 3 years'
    assert revenue.age_bucket('') == ''      # no usable invoice date
    assert revenue.age_bucket(None) == ''
    assert revenue.age_bucket(-1) == ''      # future-dated


# ── excel ────────────────────────────────────────────────────────────────────
def test_xlsx_cells_are_spelled_the_way_the_sheet_shows_them():
    openpyxl = __import__('openpyxl')
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['Invoice No.', 'Date', 'Customer Name', 'GL CODE', 'Basic Value (Rs.)'])
    ws.append(['INV1', date(2025, 4, 5), 'ACME', 4101076010, 1000])
    buf = io.BytesIO()
    wb.save(buf)

    class _X:
        def read(self):
            return buf.getvalue()

    rows, errors = revenue.parse_upload(_X())
    assert errors == []
    assert rows[0]['invoice_date'] == '2025-04-05'
    assert rows[0]['gl_code'] == '4101076010'    # not '4101076010.0'
    assert rows[0]['basic_value'] == '1000'      # not '1000.0'
