"""
Regression tests for sap_builder payload construction against the
SAP/PORTBIRD integration test cases (DPPL FY 2026-27).

These tests exercise the GST GL-account selection rules and the
credit/debit-note reference convention. They use the pure item/payload
builders with in-memory dicts (DB lookups are either bypassed by passing
svc_map directly, or monkeypatched), so no live database is required.
"""
import sap_builder


# --- service master + SAP config fixtures mirroring the tested data --------

SCRAP_SVC = {
    'sap_gl_account': '4201090080',
    'sap_igst_gl':    '1404051142',
    'sap_cgst_gl':    '1404051140',
    'sap_sgst_gl':    '1404051141',
    'sap_tds_gl':     '2206560017',
    'sap_tcs_gl':     '',
    'sap_profit_center': '510302',
    'sap_cost_center':   '',
    'service_sale_flag': 'A',
    'uom': 'MT',
}

CONFIG = {
    'company_code':        '5130',
    'business_place':      '5130',
    'section_code':        '5130',
    'credit_control_area': '5130',
    'plant_code':          '5130',
    'profit_center':       '510302',
    'igst_tax_code':       '62',
    'cgst_tax_code':       '60',
    'tds_gl':              '2206560017',
    'tcs_gl':              '',
    'round_off_gl':        '5501260001',
    'payment_term':        '',
}


def _scrap_line(**overrides):
    line = {
        'service_code': 'SCRAP',
        'line_amount':  200000.0,
        'cgst_amount':  0.0,
        'sgst_amount':  0.0,
        'igst_amount':  0.0,
        'sac_code':     '73129000',
        'unit_price':   100000.0,
        'quantity':     2.0,
        'uom':          'MT',
        'service_name': 'Scrap Sale',
    }
    line.update(overrides)
    return line


# --- TC-50 / TC-01: intra-state CGST+SGST → all three GST GLs sent ----------

def test_intrastate_cgst_sgst_sends_all_three_gst_gls():
    line = _scrap_line(cgst_amount=18000.0, sgst_amount=18000.0)
    items = sap_builder._build_items(
        [line], 'DPPL/26-27/50',
        config_defaults=CONFIG, svc_map={'SCRAP': SCRAP_SVC},
    )
    it = items[0]
    assert it['Tax_Code'] == '60'
    assert it['CGST_AMT'] == '18000.00'
    assert it['SGST_AMT'] == '18000.00'
    assert it['IGST_AMT'] == ''
    assert it['CGST_GL'] == '1404051140'
    assert it['SGST_GL'] == '1404051141'
    assert it['IGST_GL'] == '1404051142'


# --- TC-46 / TC-51: inter-state IGST → only IGST_GL, CGST/SGST GLs blank -----

def test_interstate_igst_blanks_cgst_and_sgst_gls():
    line = _scrap_line(igst_amount=36000.0)
    items = sap_builder._build_items(
        [line], 'DPPL/26-27/51',
        config_defaults=CONFIG, svc_map={'SCRAP': SCRAP_SVC},
    )
    it = items[0]
    assert it['Tax_Code'] == '62'
    assert it['IGST_AMT'] == '36000.00'
    assert it['CGST_AMT'] == ''
    assert it['SGST_AMT'] == ''
    assert it['IGST_GL'] == '1404051142'
    assert it['CGST_GL'] == ''      # must be blank for inter-state
    assert it['SGST_GL'] == ''      # must be blank for inter-state


# --- TC-09: no GST → all GST GLs and tax code blank -------------------------

def test_no_gst_blanks_all_gls_and_tax_code():
    line = _scrap_line()
    items = sap_builder._build_items(
        [line], 'DPPL/26-27/12',
        config_defaults=CONFIG, svc_map={'SCRAP': SCRAP_SVC},
    )
    it = items[0]
    assert it['Tax_Code'] == ''
    assert it['IGST_GL'] == ''
    assert it['CGST_GL'] == ''
    assert it['SGST_GL'] == ''


# --- CN against an invoice carries the ORIGINAL invoice Reference -----------
# Rule: a CN/DN against an invoice uses the same Reference as the invoice in
# both header and items; only Document_type (DG) and Invoice_Credit (C) differ.

def test_fdcn_cn_uses_original_invoice_reference_in_header_and_items(monkeypatch):
    monkeypatch.setattr(sap_builder, 'get_active_config', lambda: dict(CONFIG))
    monkeypatch.setattr(sap_builder, '_get_customer_sap_info', lambda *a, **k: {})
    monkeypatch.setattr(sap_builder, '_get_service_type_map_by_ids', lambda *a, **k: {})
    monkeypatch.setattr(sap_builder, '_get_service_gl_map', lambda *a, **k: {'SCRAP': SCRAP_SVC})

    fdcn_header = {
        'doc_number':              'DPPLCN/26-27/50',  # internal Portbird id — not sent to SAP
        'original_invoice_number': 'DPPL/26-27/50',
        'doc_date':                '2026-05-06',
        'doc_type':                'CN',
        'customer_gstin':          '27AAAAA0000A1Z5',
    }
    fdcn_line = _scrap_line(cgst_amount=18000.0, sgst_amount=18000.0, service_code='SCRAP')

    payload = sap_builder.build_fdcn_payload(fdcn_header, [fdcn_line])
    record = payload['Record_Header'][0]

    assert record['Invoice_Credit'] == 'C'
    assert record['Document_type'] == 'DG'
    assert record['Cancellation_Flag'] == ''
    assert record['Reference'] == 'DPPL/26-27/50'             # header = original invoice
    assert record['Text'] == 'DPPL/26-27/50'
    assert record['Document_Header_Text'] == 'DPPL/26-27/50'
    assert record['ITEM'][0]['Reference'] == 'DPPL/26-27/50'  # item   = original invoice


def test_fdcn_standalone_falls_back_to_doc_number(monkeypatch):
    monkeypatch.setattr(sap_builder, 'get_active_config', lambda: dict(CONFIG))
    monkeypatch.setattr(sap_builder, '_get_customer_sap_info', lambda *a, **k: {})
    monkeypatch.setattr(sap_builder, '_get_service_type_map_by_ids', lambda *a, **k: {})
    monkeypatch.setattr(sap_builder, '_get_service_gl_map', lambda *a, **k: {'SCRAP': SCRAP_SVC})

    fdcn_header = {
        'doc_number':              'DPPLDN/26-27/9',
        'original_invoice_number': '',   # no parent invoice
        'doc_date':                '2026-05-06',
        'doc_type':                'DN',
        'customer_gstin':          '27AAAAA0000A1Z5',
    }
    fdcn_line = _scrap_line(cgst_amount=18000.0, sgst_amount=18000.0, service_code='SCRAP')

    record = sap_builder.build_fdcn_payload(fdcn_header, [fdcn_line])['Record_Header'][0]
    assert record['Invoice_Credit'] == 'I'
    assert record['Document_type'] == 'DR'
    assert record['Reference'] == 'DPPLDN/26-27/9'
    assert record['ITEM'][0]['Reference'] == 'DPPLDN/26-27/9'


# --- Same-GL lines merge into a single SAP ITEM ------------------------------
# Prod case DPPL/26-27/158: five cargo lines, identical GL/tax/HSN/rate,
# must post as one line with summed Amount, GST and Quantity.

def test_same_gl_lines_merge_into_one_item():
    quantities = [7787.0, 7762.0, 3765.0, 3765.0, 7833.0]
    lines = [
        _scrap_line(
            line_amount=round(q * 174.46, 2),
            cgst_amount=round(q * 174.46 * 0.09, 2),
            sgst_amount=round(q * 174.46 * 0.09, 2),
            quantity=q, unit_price=174.46,
            service_name='Cargo Handling Unloading',
        )
        for q in quantities
    ]
    items = sap_builder._build_items(
        lines, 'DPPL/26-27/158',
        config_defaults=CONFIG, svc_map={'SCRAP': SCRAP_SVC},
    )
    assert len(items) == 1
    it = items[0]
    assert it['Amount'] == f"{sum(round(q * 174.46, 2) for q in quantities):.2f}"
    assert it['CGST_AMT'] == f"{sum(round(q * 174.46 * 0.09, 2) for q in quantities):.2f}"
    assert it['Quantity'] == f"{sum(quantities):.3f}"
    assert it['Unit_Price'] == '174.46'   # uniform rate survives the merge


def test_different_gl_lines_do_not_merge():
    other_svc = dict(SCRAP_SVC, sap_gl_account='4101076010')
    lines = [
        _scrap_line(cgst_amount=18000.0, sgst_amount=18000.0),
        _scrap_line(cgst_amount=18000.0, sgst_amount=18000.0, service_code='CHGU01'),
    ]
    items = sap_builder._build_items(
        lines, 'DPPL/26-27/158',
        config_defaults=CONFIG, svc_map={'SCRAP': SCRAP_SVC, 'CHGU01': other_svc},
    )
    assert len(items) == 2


def test_merged_mixed_rates_blank_unit_price():
    lines = [
        _scrap_line(line_amount=1000.0, quantity=10.0, unit_price=100.0),
        _scrap_line(line_amount=1100.0, quantity=10.0, unit_price=110.0),
    ]
    items = sap_builder._build_items(
        lines, 'DPPL/26-27/158',
        config_defaults=CONFIG, svc_map={'SCRAP': SCRAP_SVC},
    )
    assert len(items) == 1
    assert items[0]['Amount'] == '2100.00'
    assert items[0]['Unit_Price'] == ''   # Amount is authoritative on mixed rates


def test_round_off_lands_on_merged_first_item():
    lines = [
        _scrap_line(line_amount=1000.0, quantity=10.0),
        _scrap_line(line_amount=500.0, quantity=5.0),
    ]
    items = sap_builder._build_items(
        lines, 'DPPL/26-27/158',
        config_defaults=CONFIG, svc_map={'SCRAP': SCRAP_SVC},
        round_off=0.12,
    )
    assert len(items) == 1
    assert items[0]['Round_off_GL'] == '5501260001'
    assert items[0]['Round_off_Value'] == '0.12'
