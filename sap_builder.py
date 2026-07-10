"""
SAP / PORTBIRD DynaportInvoice JSON Payload Builder.

Spec: PORTBIRD API document (adapter doc from SAP team).

Payload envelope:  { "Record_Header": [ { ...header..., "ITEM": [...] } ] }

Header fields (PORTBIRD spec):
  Invoice_Credit         I=Invoice/DN/Reversal-of-Invoice, C=Credit Note/Reversal-of-CN
  Company_code
  Invoice_date           DD.MM.YYYY
  Posting_Date           DD.MM.YYYY (= Invoice_date)
  Reference              16 char — PMS doc number; for reversals: original SAP Document_Number
  Document_type          DR for Invoice / Debit Note, DG for Credit Note
  Customer_Code          10 char
  Invoice_Amount         13 curr (taxable + GST + TDS - TCS + Round_off, always positive)
  Business_place
  Section_code
  Text                   short narration (25 char)
  Document_Header_Text   25 char
  Payment_Term           4 char
  Credit_Control_Area    from sap_api_config.credit_control_area
  Cancellation_Flag      'X' for reversals, blank otherwise
  Nature_of_transaction  B2B / B2C
  Service_Sale           S=Service, A=Sale
  Currency               INR
  Payment_term           duplicate per spec (kept for compatibility)
  Baseline_Date          DD.MM.YYYY

Line Item fields (per spec — ITEM array; omitted entirely for reversals):
  Reference           same as header Reference (per-item)
  GL_account          SAP GL account 10 char
  Amount              taxable line amount 13 curr
  Tax_Code
  Cost_Center
  Plant
  Text                25 char description
  Profit_Center
  HSN_SAC             16 char
  CGST_AMT            blank if zero
  SGST_AMT            blank if zero
  IGST_AMT            blank if zero
  IGST_GL             10 char (blank if zero)
  SGST_GL             10 char (blank if zero)
  CGST_GL             10 char (blank if zero)
  UOM
  Unit_Price
  Quantity
  TDS_GL
  TDS_amount          blank if zero
  TCS_GL
  TCS_amount          blank if zero
  Round_off_GL        from sap_api_config.round_off_gl (blank if zero)
  Round_off_Value     header-level round_off applied to the first line item;
                      positive when invoice gross is rounded up (SAP-validated)

Reversal rule: For reversals, the payload is identical to the original
invoice with Cancellation_Flag set to 'X' and Reference set to the
original SAP Document_Number.
"""
from datetime import datetime
from database import get_db, get_cursor
from modules.SAPCFG.model import get_active_config


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_date(date_str):
    """Convert 'YYYY-MM-DD' or datetime → 'DD.MM.YYYY'."""
    if not date_str:
        return datetime.now().strftime('%d.%m.%Y')
    if isinstance(date_str, datetime):
        return date_str.strftime('%d.%m.%Y')
    date_str = str(date_str)[:10]
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').strftime('%d.%m.%Y')
    except ValueError:
        return date_str


def _fmt_amount(amount):
    """Format amount; empty string if zero/None."""
    if amount is None:
        return ''
    val = float(amount)
    return f'{val:.2f}' if val else ''


def _fmt_amount_required(amount):
    """Format amount — always returns a value (defaults to '0.00')."""
    if amount is None:
        return '0.00'
    return f'{float(amount):.2f}'


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

_CUSTOMER_TABLE_MAP = {
    'Agent':            'vessel_agents',
    'Customer':         'vessel_customers',
    'ImporterExporter': 'vessel_importer_exporters',
}


def _get_customer_sap_info(customer_type, customer_id):
    """
    Fetch both sap_customer_code (for Customer_Code field) and company_code
    (for inter-company override) in a single query.
    """
    table = _CUSTOMER_TABLE_MAP.get(customer_type)
    if not table or not customer_id:
        return {}
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute(
        f'SELECT sap_customer_code, company_code FROM {table} WHERE id = %s',
        [customer_id],
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else {}


def _nature_of_transaction(customer_gstin):
    return 'B2B' if customer_gstin and customer_gstin.strip() else 'B2C'


def _get_service_gl_map(service_codes):
    """Batch-fetch SAP-relevant fields from finance_service_types by service_code."""
    if not service_codes:
        return {}
    conn = get_db()
    cur = get_cursor(conn)
    placeholders = ','.join(['%s'] * len(service_codes))
    cur.execute(f'''
        SELECT service_code, sap_gl_account,
               sap_igst_gl, sap_cgst_gl, sap_sgst_gl,
               sap_tds_gl, sap_tcs_gl,
               sap_tax_code, sap_profit_center, sap_cost_center,
               service_sale_flag, uom
        FROM finance_service_types
        WHERE service_code IN ({placeholders})
    ''', list(service_codes))
    rows = cur.fetchall()
    conn.close()
    return {r['service_code']: dict(r) for r in rows if r['service_code']}


def _get_service_type_map_by_ids(type_ids):
    """Batch-fetch SAP-relevant fields from finance_service_types by integer id."""
    if not type_ids:
        return {}
    conn = get_db()
    cur = get_cursor(conn)
    placeholders = ','.join(['%s'] * len(type_ids))
    cur.execute(f'''
        SELECT id, service_code, sap_gl_account,
               sap_igst_gl, sap_cgst_gl, sap_sgst_gl,
               sap_tds_gl, sap_tcs_gl,
               sap_tax_code, sap_profit_center, sap_cost_center,
               service_sale_flag, uom
        FROM finance_service_types
        WHERE id IN ({placeholders})
    ''', list(type_ids))
    rows = cur.fetchall()
    conn.close()
    return {r['id']: dict(r) for r in rows}


def _service_sale_flag(lines, svc_map):
    """Derive header-level Service_Sale from the first line that has a service master entry."""
    for line in lines:
        svc_code = line.get('service_code') or ''
        flag = (
            line.get('service_sale_flag')
            or svc_map.get(svc_code, {}).get('service_sale_flag')
        )
        if flag:
            return flag
    return 'S'


# ---------------------------------------------------------------------------
# Item builder (shared by all non-reversal document types)
# ---------------------------------------------------------------------------

# Fields that are summed when merging; everything else must match for two
# items to collapse into one SAP line (GL, tax code, HSN, GST/TDS/TCS GLs,
# centers, plant, text, UOM).
_MERGE_SUM_FIELDS = ('CGST_AMT', 'SGST_AMT', 'IGST_AMT', 'TDS_amount', 'TCS_amount')
_MERGE_SKIP_FIELDS = _MERGE_SUM_FIELDS + (
    'Amount', 'Quantity', 'Unit_Price', 'Round_off_GL', 'Round_off_Value')


def _merge_same_gl_items(items):
    """Collapse items posting to the same GL into a single SAP line.

    SAP expects one ITEM per GL account: amounts, GST, TDS/TCS and quantity
    are summed. Unit_Price survives only when uniform across the merged
    lines — Amount is authoritative, so mixed rates leave it blank.
    """
    merged = {}
    for it in items:
        key = tuple(v for k, v in it.items() if k not in _MERGE_SKIP_FIELDS)
        tgt = merged.get(key)
        if tgt is None:
            merged[key] = it
            continue
        tgt['Amount'] = _fmt_amount_required(float(tgt['Amount'] or 0) + float(it['Amount'] or 0))
        for f in _MERGE_SUM_FIELDS:
            tgt[f] = _fmt_amount(float(tgt[f] or 0) + float(it[f] or 0))
        if tgt['Quantity'] and it['Quantity']:
            tgt['Quantity'] = f"{float(tgt['Quantity']) + float(it['Quantity']):.3f}"
        else:
            tgt['Quantity'] = ''
        if tgt['Unit_Price'] != it['Unit_Price']:
            tgt['Unit_Price'] = ''
    return list(merged.values())

def _build_items(lines, reference, amount_field='line_amount',
                 config_defaults=None, svc_map=None, doc_type='DR',
                 round_off=0):
    """
    Build the ITEM list per PORTBIRD spec.

    Reference     — per-line, mirrors header Reference (PMS doc number)
    GL sources    — service master (sap_gl_account, sap_igst_gl, sap_cgst_gl,
                    sap_sgst_gl, sap_tds_gl, sap_tcs_gl) with SAP config fallback
                    for TDS/TCS/round-off.
    round_off     — header-level round-off amount; applied to the FIRST item
                    only (positive sign) so SAP-side debit/credit balances.
    """
    config_defaults = config_defaults or {}

    if svc_map is None:
        service_codes = {l.get('service_code') for l in lines if l.get('service_code')}
        svc_map = _get_service_gl_map(service_codes)

    items = []
    for line in lines:
        taxable = float(line.get(amount_field) or 0)
        cgst    = float(line.get('cgst_amount') or 0)
        sgst    = float(line.get('sgst_amount') or 0)
        igst    = float(line.get('igst_amount') or 0)

        svc_code = line.get('service_code') or ''
        svc      = svc_map.get(svc_code, {})

        # GL_account: strictly from a real GL field. Never fall back to
        # service_code (that's an HSN/SAC code, not a GL — would post wrong).
        #   1. Service master's sap_gl_account (FSTM01) — authoritative
        #   2. Line-level gl_code copied from the bill (snapshot at billing time)
        gl_account = svc.get('sap_gl_account') or line.get('gl_code') or ''
        plant      = line.get('plant') or config_defaults.get('plant_code') or ''

        igst_gl = svc.get('sap_igst_gl') or ''
        cgst_gl = svc.get('sap_cgst_gl') or ''
        sgst_gl = svc.get('sap_sgst_gl') or ''

        uom        = line.get('uom') or svc.get('uom') or ''
        unit_price = line.get('unit_price') if line.get('unit_price') is not None else line.get('rate')
        quantity   = line.get('quantity')

        # TDS / TCS GL only when applicable on the line (or amount actually present).
        tds_amount_val = float(line.get('tds_amount') or 0)
        tcs_amount_val = float(line.get('tcs_amount') or 0)
        tds_applicable = bool(int(line.get('tds_applicable') or 0)) or tds_amount_val > 0
        tcs_applicable = bool(int(line.get('tcs_applicable') or 0)) or tcs_amount_val > 0
        tds_gl       = (svc.get('sap_tds_gl') or config_defaults.get('tds_gl') or '') if tds_applicable else ''
        tcs_gl       = (svc.get('sap_tcs_gl') or config_defaults.get('tcs_gl') or '') if tcs_applicable else ''

        # Tax_Code is empty when the line has no GST (TC-09 spec).
        # When GST applies, pick by transaction type:
        #   IGST > 0   → inter-state → igst_tax_code
        #   CGST/SGST  → intra-state → cgst_tax_code
        if igst > 0:
            tax_code = config_defaults.get('igst_tax_code') or config_defaults.get('tax_code') or ''
        elif (cgst + sgst) > 0:
            tax_code = config_defaults.get('cgst_tax_code') or config_defaults.get('tax_code') or ''
        else:
            tax_code = ''

        # GST GL accounts follow the transaction type (matches SAP-tested payloads):
        #   inter-state (IGST)   → only IGST_GL; CGST_GL / SGST_GL stay blank
        #   intra-state (C/SGST) → all three GLs sent together (incl. IGST_GL)
        #   no GST               → all three blank
        if igst > 0:
            igst_gl_out = igst_gl[:10] if igst_gl else ''
            cgst_gl_out = ''
            sgst_gl_out = ''
        elif (cgst + sgst) > 0:
            igst_gl_out = igst_gl[:10] if igst_gl else ''
            cgst_gl_out = cgst_gl[:10] if cgst_gl else ''
            sgst_gl_out = sgst_gl[:10] if sgst_gl else ''
        else:
            igst_gl_out = cgst_gl_out = sgst_gl_out = ''
        profit_center = (
            line.get('profit_center')
            or svc.get('sap_profit_center')
            or config_defaults.get('profit_center')
            or ''
        )
        cost_center = (
            line.get('cost_center')
            or svc.get('sap_cost_center')
            or ''
        )

        items.append({
            'Reference':        (reference or '')[:16],
            'GL_account':       gl_account[:10],
            'Amount':           _fmt_amount_required(taxable),
            'Tax_Code':         tax_code,
            'Cost_Center':      cost_center,
            'Plant':            plant,
            'Text':             (line.get('service_name') or '')[:25],
            'Profit_Center':    profit_center,
            'HSN_SAC':          (line.get('sac_code') or line.get('hsn_sac') or '')[:16],
            'CGST_AMT':         _fmt_amount(cgst),
            'SGST_AMT':         _fmt_amount(sgst),
            'IGST_AMT':         _fmt_amount(igst),
            'IGST_GL':          igst_gl_out,
            'SGST_GL':          sgst_gl_out,
            'CGST_GL':          cgst_gl_out,
            'UOM':              uom,
            'Unit_Price':       _fmt_amount_required(unit_price) if unit_price is not None else '',
            'Quantity':         f'{float(quantity):.3f}' if quantity is not None else '',
            'TDS_GL':           tds_gl,
            'TDS_amount':       _fmt_amount(line.get('tds_amount')),
            'TCS_GL':           tcs_gl,
            'TCS_amount':       _fmt_amount(line.get('tcs_amount')),
            'Round_off_GL':     '',
            'Round_off_Value':  '',
        })

    # Merge lines posting to the same GL before round-off placement.
    items = _merge_same_gl_items(items)

    # Apply header-level round-off to the first item (positive sign, per SAP).
    if items and float(round_off or 0):
        items[0]['Round_off_GL']    = config_defaults.get('round_off_gl') or ''
        items[0]['Round_off_Value'] = _fmt_amount(round_off)

    return items


def _total_invoice_amount(header, lines, amount_field='line_amount'):
    """Return net invoice value (taxable + GST + TDS - TCS + Round_off).

    `total_amount` in invoice headers stores taxable + GST only (verified in
    FIN01/FDCN01 model layers), so TDS/TCS/round-off are added on top.
    """
    total = float(header.get('total_amount') or 0)
    if not total:
        total  = sum(float(l.get(amount_field) or 0) for l in lines)
        total += sum(float(l.get('cgst_amount') or 0) for l in lines)
        total += sum(float(l.get('sgst_amount') or 0) for l in lines)
        total += sum(float(l.get('igst_amount') or 0) for l in lines)

    tds = float(header.get('tds_amount') or 0)
    if not tds:
        tds = sum(float(l.get('tds_amount') or 0) for l in lines)
    tcs = float(header.get('tcs_amount') or 0)
    if not tcs:
        tcs = sum(float(l.get('tcs_amount') or 0) for l in lines)
    round_off = float(header.get('round_off') or 0)

    return total + tds - tcs + round_off


# ---------------------------------------------------------------------------
# Common header builder
# ---------------------------------------------------------------------------

def _build_header_base(config, customer_code, company, inv_date,
                       reference, header_text, short_text, currency,
                       invoice_credit, document_type, customer_gstin,
                       service_sale, invoice_amount):
    """Construct the PORTBIRD header dict (shared by invoice/DN/CN/reversal)."""
    payment_term         = (config.get('default_payment_term') or config.get('payment_term') or '')[:4]
    business_place       = config.get('business_place') or company
    section_code         = config.get('section_code')   or company
    credit_control_area  = config.get('credit_control_area') or company

    return {
        'Invoice_Credit':        invoice_credit,
        'Company_code':          company,
        'Invoice_date':          inv_date,
        'Posting_Date':          inv_date,
        'Reference':             (reference or '')[:16],
        'Document_type':         document_type,
        'Customer_Code':         (customer_code or '')[:10],
        'Invoice_Amount':        _fmt_amount_required(invoice_amount),
        'Business_place':        business_place,
        'Section_code':          section_code,
        'Text':                  (short_text or '')[:25],
        'Document_Header_Text':  (header_text or '')[:25],
        'Payment_Term':          payment_term,
        'Credit_Control_Area':   credit_control_area,
        'Cancellation_Flag':     '',
        'Nature_of_transaction': _nature_of_transaction(customer_gstin),
        'Service_Sale':          service_sale,
        'Currency':              currency or 'INR',
        'Payment_term':          payment_term,
        'Baseline_Date':         inv_date,
    }


# ---------------------------------------------------------------------------
# Invoice builder  (Invoice_Credit = I, Document_type = INV)
# ---------------------------------------------------------------------------

def build_invoice_payload(invoice_header, invoice_lines):
    config = get_active_config()
    if not config:
        raise ValueError('No active SAP configuration found')

    default_company = config.get('company_code', '5171')

    cust_info = _get_customer_sap_info(
        invoice_header.get('customer_type'),
        invoice_header.get('customer_id'),
    )
    company       = cust_info.get('company_code') or default_company
    customer_code = cust_info.get('sap_customer_code') or invoice_header.get('customer_gl_code') or ''
    inv_date      = _fmt_date(invoice_header.get('invoice_date'))
    invoice_no    = invoice_header.get('invoice_number') or ''

    svc_codes = {l.get('service_code') for l in invoice_lines if l.get('service_code')}
    svc_map   = _get_service_gl_map(svc_codes)

    record = _build_header_base(
        config=config,
        customer_code=customer_code,
        company=company,
        inv_date=inv_date,
        reference=invoice_no,
        header_text=invoice_no,
        short_text=invoice_no,
        currency=invoice_header.get('currency_code'),
        invoice_credit='I',
        document_type='DR',
        customer_gstin=invoice_header.get('customer_gstin'),
        service_sale=_service_sale_flag(invoice_lines, svc_map),
        invoice_amount=_total_invoice_amount(invoice_header, invoice_lines),
    )
    record['ITEM'] = _build_items(
        invoice_lines, invoice_no,
        config_defaults=config, svc_map=svc_map, doc_type='DR',
        round_off=float(invoice_header.get('round_off') or 0),
    )
    return {'Record_Header': [record]}


# ---------------------------------------------------------------------------
# FDCN01 Debit / Credit Note builder
# ---------------------------------------------------------------------------

def build_fdcn_payload(fdcn_header, fdcn_lines):
    config = get_active_config()
    if not config:
        raise ValueError('No active SAP configuration found')

    default_company = config.get('company_code', '5171')

    cust_info = _get_customer_sap_info(
        fdcn_header.get('customer_type'),
        fdcn_header.get('customer_id'),
    )
    company       = cust_info.get('company_code') or default_company
    customer_code = cust_info.get('sap_customer_code') or fdcn_header.get('customer_gl_code') or ''
    doc_date      = _fmt_date(fdcn_header.get('doc_date'))
    doc_number    = fdcn_header.get('doc_number') or ''
    doc_type      = fdcn_header.get('doc_type', 'CN')   # 'DN' or 'CN'

    # A CN/DN raised against an invoice carries the ORIGINAL invoice's Reference
    # in both the header and the line items — same number as the invoice, with
    # only Document_type (DG/DR) and Invoice_Credit (C/I) distinguishing it.
    # Standalone notes with no parent invoice fall back to the FDCN doc_number.
    reference = fdcn_header.get('original_invoice_number') or doc_number

    # PORTBIRD Document_type: DN → 'DR' (debit), CN → 'DG' (credit)
    # Invoice_Credit: DN → 'I' (adds to receivable), CN → 'C' (reduces receivable)
    document_type  = 'DR' if doc_type == 'DN' else 'DG'
    invoice_credit = 'I' if doc_type == 'DN' else 'C'

    # fdcn_lines store service_type_id (integer FK), not service_code (string).
    type_ids = {l.get('service_type_id') for l in fdcn_lines if l.get('service_type_id')}
    type_map = _get_service_type_map_by_ids(type_ids)
    enriched_lines = []
    for line in fdcn_lines:
        l = dict(line)
        if not l.get('service_code'):
            tid = l.get('service_type_id')
            svc_type = type_map.get(tid, {})
            l['service_code'] = svc_type.get('service_code') or l.get('gl_code') or ''
        enriched_lines.append(l)

    svc_codes = {l.get('service_code') for l in enriched_lines if l.get('service_code')}
    svc_map   = _get_service_gl_map(svc_codes)

    record = _build_header_base(
        config=config,
        customer_code=customer_code,
        company=company,
        inv_date=doc_date,
        reference=reference,
        header_text=reference,
        short_text=reference,
        currency='INR',
        invoice_credit=invoice_credit,
        document_type=document_type,
        customer_gstin=fdcn_header.get('customer_gstin'),
        service_sale=_service_sale_flag(enriched_lines, svc_map),
        invoice_amount=_total_invoice_amount(fdcn_header, enriched_lines),
    )
    record['ITEM'] = _build_items(
        enriched_lines, reference,
        config_defaults=config, svc_map=svc_map, doc_type=doc_type,
        round_off=float(fdcn_header.get('round_off') or 0),
    )
    return {'Record_Header': [record]}


# ---------------------------------------------------------------------------
# Invoice reversal builder  (same as invoice with Cancellation_Flag = 'X')
# ---------------------------------------------------------------------------

def build_invoice_reversal_payload(invoice_header, invoice_lines):
    """
    FB08 reversal payload (within 24 hours of posting): identical shape to
    the original invoice payload, only Cancellation_Flag = 'X'. Reference
    stays the PMS invoice_number — SAP looks up the original posted doc
    by reference, not by SAP doc number.
    """
    config = get_active_config()
    if not config:
        raise ValueError('No active SAP configuration found')

    default_company = config.get('company_code', '5171')

    cust_info = _get_customer_sap_info(
        invoice_header.get('customer_type'),
        invoice_header.get('customer_id'),
    )
    company       = cust_info.get('company_code') or default_company
    customer_code = cust_info.get('sap_customer_code') or invoice_header.get('customer_gl_code') or ''
    inv_date      = _fmt_date(invoice_header.get('invoice_date'))

    original_ref = invoice_header.get('invoice_number') or ''

    svc_codes = {l.get('service_code') for l in invoice_lines if l.get('service_code')}
    svc_map   = _get_service_gl_map(svc_codes)

    record = _build_header_base(
        config=config,
        customer_code=customer_code,
        company=company,
        inv_date=inv_date,
        reference=original_ref,
        header_text=original_ref,
        short_text=original_ref,
        currency=invoice_header.get('currency_code'),
        invoice_credit='I',
        document_type='DR',
        customer_gstin=invoice_header.get('customer_gstin'),
        service_sale=_service_sale_flag(invoice_lines, svc_map),
        invoice_amount=_total_invoice_amount(invoice_header, invoice_lines),
    )
    record['Cancellation_Flag'] = 'X'
    record['ITEM'] = _build_items(
        invoice_lines, original_ref,
        config_defaults=config, svc_map=svc_map, doc_type='DR',
        round_off=float(invoice_header.get('round_off') or 0),
    )
    return {'Record_Header': [record]}


# ---------------------------------------------------------------------------
# Invoice credit-note builder (post 24-hour cancellation, FB08 window expired)
# ---------------------------------------------------------------------------

def build_invoice_credit_note_payload(invoice_header, invoice_lines):
    """
    Post-24-hour cancellation payload — issued against the original invoice
    when the FB08 reversal window has expired. Same shape as the original
    invoice payload but with Invoice_Credit='C', Document_type='DG' and
    Cancellation_Flag blank. Reference stays the original PMS invoice_number.
    """
    config = get_active_config()
    if not config:
        raise ValueError('No active SAP configuration found')

    default_company = config.get('company_code', '5171')

    cust_info = _get_customer_sap_info(
        invoice_header.get('customer_type'),
        invoice_header.get('customer_id'),
    )
    company       = cust_info.get('company_code') or default_company
    customer_code = cust_info.get('sap_customer_code') or invoice_header.get('customer_gl_code') or ''
    inv_date      = _fmt_date(invoice_header.get('invoice_date'))
    invoice_no    = invoice_header.get('invoice_number') or ''

    svc_codes = {l.get('service_code') for l in invoice_lines if l.get('service_code')}
    svc_map   = _get_service_gl_map(svc_codes)

    record = _build_header_base(
        config=config,
        customer_code=customer_code,
        company=company,
        inv_date=inv_date,
        reference=invoice_no,
        header_text=invoice_no,
        short_text=invoice_no,
        currency=invoice_header.get('currency_code'),
        invoice_credit='C',
        document_type='DG',
        customer_gstin=invoice_header.get('customer_gstin'),
        service_sale=_service_sale_flag(invoice_lines, svc_map),
        invoice_amount=_total_invoice_amount(invoice_header, invoice_lines),
    )
    record['ITEM'] = _build_items(
        invoice_lines, invoice_no,
        config_defaults=config, svc_map=svc_map, doc_type='DG',
        round_off=float(invoice_header.get('round_off') or 0),
    )
    return {'Record_Header': [record]}
