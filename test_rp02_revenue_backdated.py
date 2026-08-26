"""Pure-function tests for the RP02 backdated revenue upload (no DB).

The template the exporter hands out must survive a round trip through the
parser — that is the whole contract of the uploader.
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
    assert r['invoice_date'] == date(2025, 4, 5)
    assert r['customer_name'] == 'JSW Steel Ltd'
    assert r['grouping_label'] == 'Cargo Handling Charges'
    assert r['basic_value'] == 2340000.0
    assert r['net_receivable'] == 2714400.0
    assert r['booking_status'] == 'Booked'
    assert r['cargo_volume'] == 'YES'      # a flag in the sheet, not a quantity


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
           'INV1,05-04-2025,ACME,4101076010,"1,000.00"\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert rows[0]['basic_value'] == 1000.0


def test_unrecognisable_file_reports_a_missing_header_row():
    rows, errors = _parse('just,some,junk\n1,2,3\n')
    assert rows == []
    assert 'header row' in errors[0]['message']


# ── validation ───────────────────────────────────────────────────────────────
def test_required_fields_are_reported_with_the_spreadsheet_row_number():
    csv = ('Invoice No.,Date,Customer Name,GL CODE,Basic Value (Rs.)\n'
           ',05-04-2025,ACME,4101076010,100\n'
           'INV2,,ACME,4101076010,100\n'
           'INV3,05-04-2025,,4101076010,100\n')
    rows, errors = _parse(csv)
    assert rows == []
    assert [e['row'] for e in errors] == [2, 3, 4]
    assert 'Invoice No. is required' in errors[0]['message']
    assert 'Date is required' in errors[1]['message']
    assert 'Customer Name is required' in errors[2]['message']


def test_bad_number_and_bad_date_are_rejected_by_column_name():
    csv = ('Invoice No.,Date,Customer Name,Basic Value (Rs.)\n'
           'INV1,05-04-2025,ACME,not-a-number\n'
           'INV2,31-31-2025,ACME,100\n')
    rows, errors = _parse(csv)
    assert rows == []
    assert 'Basic Value (Rs.)' in errors[0]['message']
    assert 'invalid date' in errors[1]['message']


def test_blank_rows_are_skipped():
    csv = ('Invoice No.,Date,Customer Name,GL CODE,Basic Value (Rs.)\n'
           ',,,,\n'
           'INV1,05-04-2025,ACME,4101076010,100\n')
    rows, errors = _parse(csv)
    assert errors == []
    assert len(rows) == 1


# ── derived money fields ─────────────────────────────────────────────────────
def test_blank_tds_is_filled_from_the_gl_code_rate():
    csv = ('Invoice No.,Date,Customer Name,GL CODE,Basic Value (Rs.),Invoice value (Rs.)\n'
           'INV1,05-04-2025,ACME,4101076010,1000.00,1180.00\n')
    rows, _ = _parse(csv)
    assert rows[0]['tds_tcs'] == 20.0            # 2% of the basic value
    assert rows[0]['net_receivable'] == 1160.0   # invoice value less TDS


def test_tds_in_the_file_is_kept_as_uploaded():
    csv = ('Invoice No.,Date,Customer Name,GL CODE,Basic Value (Rs.),'
           'Invoice value (Rs.),TDS/TCS,NET RECEIVABLE\n'
           'INV1,05-04-2025,ACME,4101076010,1000.00,1180.00,5.00,1175.00\n')
    rows, _ = _parse(csv)
    assert rows[0]['tds_tcs'] == 5.0
    assert rows[0]['net_receivable'] == 1175.0


def test_unknown_gl_code_gets_no_tds():
    csv = ('Invoice No.,Date,Customer Name,GL CODE,Basic Value (Rs.),Invoice value (Rs.)\n'
           'INV1,05-04-2025,ACME,9999999999,1000.00,1180.00\n')
    rows, _ = _parse(csv)
    assert rows[0]['tds_tcs'] == 0.0
    assert rows[0]['net_receivable'] == 1180.0


# ── dates and ageing ─────────────────────────────────────────────────────────
def test_common_date_formats_all_land_on_the_same_day():
    for text in ('2025-04-05', '05-04-2025', '05/04/2025', '05.04.2025', '05-Apr-2025'):
        assert revenue.parse_date(text) == date(2025, 4, 5)


def test_parse_date_blank_is_none_and_junk_raises():
    assert revenue.parse_date('') is None
    assert revenue.parse_date(None) is None
    try:
        revenue.parse_date('tomorrow')
        assert False, 'expected ValueError'
    except ValueError:
        pass


def test_age_buckets_at_the_boundaries():
    assert revenue.age_bucket(0) == '0-30 days'
    assert revenue.age_bucket(30) == '0-30 days'
    assert revenue.age_bucket(31) == '31-60 days'
    assert revenue.age_bucket(1095) == '2-3 years'
    assert revenue.age_bucket(1096) == '> 3 years'
    assert revenue.age_bucket('') == ''      # live rows with no invoice date
    assert revenue.age_bucket(None) == ''
    assert revenue.age_bucket(-1) == ''      # future-dated


# ── excel ────────────────────────────────────────────────────────────────────
def test_xlsx_upload_parses_like_csv():
    openpyxl = __import__('openpyxl')
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['Invoice No.', 'Date', 'Customer Name', 'GL CODE', 'Basic Value (Rs.)'])
    ws.append(['INV1', date(2025, 4, 5), 'ACME', '4101076010', 1000])
    buf = io.BytesIO()
    wb.save(buf)

    class _X:
        def read(self):
            return buf.getvalue()

    rows, errors = revenue.parse_upload(_X())
    assert errors == []
    assert rows[0]['invoice_date'] == date(2025, 4, 5)
    assert rows[0]['basic_value'] == 1000.0
