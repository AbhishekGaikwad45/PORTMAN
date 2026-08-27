"""
GST e-Invoice JSON Builder for IRP (Invoice Registration Portal).

Builds the e-invoice payload per NIC IRP schema v1.03 from
invoice_header + invoice_lines or credit_note_header + credit_note_lines.

Key sections:
- TranDtls: Transaction details (supply type, category)
- DocDtls: Document details (type, number, date)
- SellerDtls: Seller (our company) details
- BuyerDtls: Buyer (customer) details
- ItemList: Line items with HSN/SAC, quantity, rates, GST breakup
- ValDtls: Value totals (assessable, CGST, SGST, IGST, total)
"""
from datetime import datetime
from database import get_db, get_cursor


# ---------------------------------------------------------------------------
# Seller defaults (loaded once from config or hardcoded for now)
# ---------------------------------------------------------------------------
_SELLER_DEFAULTS = {
    'Gstin': '',       # Will be filled from GST config
    'LglNm': 'Dyna Logistics Pvt Ltd',
    'TrdNm': 'Dyna Logistics Pvt Ltd',
    'Addr1': '',
    'Loc': '',
    'Pin': 0,
    'Stcd': '',        # State code e.g. "27"
}


def _get_seller_details():
    """Load seller GSTIN from active GST config."""
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute('SELECT * FROM gst_api_config WHERE is_active=1 LIMIT 1')
    row = cur.fetchone()
    conn.close()

    seller = dict(_SELLER_DEFAULTS)
    if row:
        seller['Gstin'] = row.get('gstin') or ''
        # State code = first 2 digits of GSTIN
        gstin = seller['Gstin']
        if gstin and len(gstin) >= 2:
            seller['Stcd'] = gstin[:2]
    return seller


def _fmt_date(date_str):
    """Convert 'YYYY-MM-DD' to 'DD/MM/YYYY' (IRP format)."""
    if not date_str:
        return datetime.now().strftime('%d/%m/%Y')
    date_str = str(date_str)[:10]
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return dt.strftime('%d/%m/%Y')
    except ValueError:
        return date_str


def _safe_float(val):
    """Convert to float, defaulting to 0."""
    try:
        return round(float(val), 2) if val else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_int(val):
    try:
        return int(val) if val else 0
    except (TypeError, ValueError):
        return 0


def _get_buyer_state_code(customer_gst_state_code, customer_gstin):
    """Determine buyer state code from state_code field or GSTIN prefix."""
    if customer_gst_state_code:
        return str(customer_gst_state_code)
    if customer_gstin and len(customer_gstin) >= 2:
        return customer_gstin[:2]
    return ''


# ---------------------------------------------------------------------------
# Invoice builder
# ---------------------------------------------------------------------------
def build_einvoice_from_invoice(invoice_header, invoice_lines):
    """
    Build IRP e-invoice JSON from invoice_header + invoice_lines dicts.

    Returns dict ready for gsp_client.generate_irn().
    """
    seller = _get_seller_details()
    buyer_state = _get_buyer_state_code(
        invoice_header.get('customer_gst_state_code'),
        invoice_header.get('customer_gstin')
    )
    seller_state = seller.get('Stcd', '')

    # Determine supply type: intra-state vs inter-state
    is_intra = (seller_state == buyer_state) if seller_state and buyer_state else True
    supply_type = 'B2B'  # Business to Business

    # --- TranDtls ---
    tran_dtls = {
        'TaxSch': 'GST',
        'SupTyp': supply_type,
        'RegRev': 'N',     # Regular (not reverse charge)
        'IgstOnIntra': 'N',
    }

    # --- DocDtls ---
    doc_dtls = {
        'Typ': 'INV',
        'No': invoice_header.get('invoice_number', ''),
        'Dt': _fmt_date(invoice_header.get('invoice_date')),
    }

    # --- SellerDtls ---
    seller_dtls = {
        'Gstin': seller['Gstin'],
        'LglNm': seller['LglNm'],
        'TrdNm': seller['TrdNm'],
        'Addr1': seller.get('Addr1', ''),
        'Loc': seller.get('Loc', ''),
        'Pin': _safe_int(seller.get('Pin', 0)),
        'Stcd': seller_state,
    }

    # --- BuyerDtls ---
    buyer_dtls = {
        'Gstin': invoice_header.get('customer_gstin') or 'URP',
        'LglNm': invoice_header.get('customer_name', ''),
        'TrdNm': invoice_header.get('customer_name', ''),
        'Addr1': '',
        'Loc': '',
        'Pin': 0,
        'Stcd': buyer_state,
        'Pos': buyer_state,  # Place of supply
    }

    # --- ItemList ---
    item_list = []
    total_assessable = 0.0
    total_cgst = 0.0
    total_sgst = 0.0
    total_igst = 0.0
    total_invoice = 0.0

    for idx, line in enumerate(invoice_lines, start=1):
        line_amt = _safe_float(line.get('line_amount'))
        cgst_amt = _safe_float(line.get('cgst_amount'))
        sgst_amt = _safe_float(line.get('sgst_amount'))
        igst_amt = _safe_float(line.get('igst_amount'))
        line_total = _safe_float(line.get('line_total'))

        cgst_rate = _safe_float(line.get('cgst_rate'))
        sgst_rate = _safe_float(line.get('sgst_rate'))
        igst_rate = _safe_float(line.get('igst_rate'))
        gst_rate = cgst_rate + sgst_rate + igst_rate

        item = {
            'SlNo': str(idx),
            'PrdDesc': (line.get('service_name') or '')[:300],
            'IsServc': 'Y',  # Service
            'HsnCd': line.get('sac_code') or '999799',  # SAC code as HSN
            'Qty': _safe_float(line.get('quantity')) or 1.0,
            'Unit': _map_uom(line.get('uom')),
            'UnitPrice': _safe_float(line.get('rate')),
            'TotAmt': line_amt,
            'Discount': 0,
            'AssAmt': line_amt,       # Assessable amount
            'GstRt': gst_rate,
            'CgstAmt': cgst_amt,
            'SgstAmt': sgst_amt,
            'IgstAmt': igst_amt,
            'CesRt': 0,
            'CesAmt': 0,
            'CesNonAdvlAmt': 0,
            'StateCesRt': 0,
            'StateCesAmt': 0,
            'StateCesNonAdvlAmt': 0,
            'OthChrg': 0,
            'TotItemVal': line_total,
        }
        item_list.append(item)

        total_assessable += line_amt
        total_cgst += cgst_amt
        total_sgst += sgst_amt
        total_igst += igst_amt
        total_invoice += line_total

    # --- ValDtls ---
    # TCS is charged on the invoice, so the IRP sees it as an other-charge and
    # it has to be inside TotInvVal — the portal validates
    # TotInvVal ~= AssVal + taxes + OthChrg and rejects a total that omits it.
    tcs = _safe_float(invoice_header.get('tcs_amount'))
    round_off = _safe_float(invoice_header.get('round_off'))
    val_dtls = {
        'AssVal': round(total_assessable, 2),
        'CgstVal': round(total_cgst, 2),
        'SgstVal': round(total_sgst, 2),
        'IgstVal': round(total_igst, 2),
        'CesVal': 0,
        'StCesVal': 0,
        'Discount': 0,
        'OthChrg': round(tcs, 2),
        'RndOffAmt': round(round_off, 2),
        'TotInvVal': round(total_invoice + tcs + round_off, 2),
    }

    return {
        'Version': '1.1',
        'TranDtls': tran_dtls,
        'DocDtls': doc_dtls,
        'SellerDtls': seller_dtls,
        'BuyerDtls': buyer_dtls,
        'ItemList': item_list,
        'ValDtls': val_dtls,
    }


# ---------------------------------------------------------------------------
# Credit Note builder
# ---------------------------------------------------------------------------
def build_einvoice_from_credit_note(cn_header, cn_lines):
    """
    Build IRP e-invoice JSON for a Credit Note.

    Same structure as invoice but DocDtls.Typ = 'CRN'.
    """
    result = build_einvoice_from_invoice(cn_header, cn_lines)

    # Override document type
    result['DocDtls']['Typ'] = 'CRN'
    result['DocDtls']['No'] = cn_header.get('credit_note_number', '')
    result['DocDtls']['Dt'] = _fmt_date(cn_header.get('credit_note_date'))

    # Add original invoice reference if available
    if cn_header.get('original_invoice_number'):
        result['PrecDocDtls'] = [{
            'InvNo': cn_header['original_invoice_number'],
            'InvDt': _fmt_date(cn_header.get('original_invoice_date')),
        }]

    return result


# ---------------------------------------------------------------------------
# FDCN01 Debit/Credit Note builder
# ---------------------------------------------------------------------------
def build_einvoice_from_fdcn(fdcn_header, fdcn_lines):
    """
    Build IRP e-invoice JSON for a Debit Note or Credit Note (FDCN01).

    Debit Note: DocDtls.Typ = 'DBN'
    Credit Note: DocDtls.Typ = 'CRN'
    Includes PrecDocDtls referencing the original invoice.
    """
    # Map fdcn fields to the format expected by build_einvoice_from_invoice
    mapped_header = {
        'invoice_number': fdcn_header.get('doc_number', ''),
        'invoice_date': fdcn_header.get('doc_date'),
        'customer_gstin': fdcn_header.get('customer_gstin'),
        'customer_gst_state_code': fdcn_header.get('customer_gst_state_code'),
        'customer_name': fdcn_header.get('customer_name'),
    }

    # Map lines — use line_amount, line_total, and GST fields as-is
    mapped_lines = []
    for line in fdcn_lines:
        mapped_lines.append({
            'service_name': line.get('service_name', ''),
            'sac_code': line.get('sac_code'),
            'quantity': line.get('quantity'),
            'uom': line.get('uom'),
            'rate': abs(float(line.get('rate_difference') or 0)),
            'line_amount': line.get('line_amount'),
            'cgst_rate': line.get('cgst_rate'),
            'sgst_rate': line.get('sgst_rate'),
            'igst_rate': line.get('igst_rate'),
            'cgst_amount': line.get('cgst_amount'),
            'sgst_amount': line.get('sgst_amount'),
            'igst_amount': line.get('igst_amount'),
            'line_total': line.get('line_total'),
        })

    result = build_einvoice_from_invoice(mapped_header, mapped_lines)

    # Override document type based on DN or CN
    is_debit = fdcn_header.get('doc_type') == 'DN'
    result['DocDtls']['Typ'] = 'DBN' if is_debit else 'CRN'
    result['DocDtls']['No'] = fdcn_header.get('doc_number', '')
    result['DocDtls']['Dt'] = _fmt_date(fdcn_header.get('doc_date'))

    # Add original invoice reference
    orig_inv_number = fdcn_header.get('original_invoice_number') or \
                      fdcn_header.get('original_invoice_number_display') or ''
    if orig_inv_number:
        result['PrecDocDtls'] = [{
            'InvNo': orig_inv_number,
            'InvDt': _fmt_date(fdcn_header.get('original_invoice_date')),
        }]

    return result


# ---------------------------------------------------------------------------
# UOM mapping (IRP uses specific unit codes)
# ---------------------------------------------------------------------------
_UOM_MAP = {
    'MT': 'MTS',       # Metric Ton
    'KG': 'KGS',       # Kilogram
    'LTR': 'LTR',      # Litre
    'NOS': 'NOS',      # Numbers
    'HRS': 'HRS',      # Hours
    'DAYS': 'DAY',     # Days
    'SQM': 'SQM',      # Square Metre
    'CBM': 'CBM',      # Cubic Metre
    'KM': 'KLR',       # Kilometre
    'LOT': 'LOT',      # Lot
    'SET': 'SET',       # Set
    'PCS': 'PCS',      # Pieces
}


def _map_uom(uom):
    """Map PORTMAN UOM to IRP unit code. Default OTH (Others)."""
    if not uom:
        return 'OTH'
    return _UOM_MAP.get(uom.upper(), 'OTH')
