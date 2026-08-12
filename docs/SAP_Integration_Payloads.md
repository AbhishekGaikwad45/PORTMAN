# SAP Integration - Payload Reference

> **Status: Draft** — The SAP PI/PO interface is not yet developed. This document defines the agreed staging table structure and payload format for implementation. Field values and field names are subject to change pending SAP team confirmation.

## Overview

Portbird will communicate with SAP ECC via a SAP PI/PO REST adapter. All supported documents will be posted as JSON to a single endpoint, and PI middleware will transform them into the appropriate SAP document type.

**Endpoint:** `{base_url}/RESTAdapter/Portbirdinvoice`  
**Method:** `POST`  
**Auth:** OAuth2 client credentials (`/oauth2/api/v1/generateToken`)  

All payloads share the same outer envelope:

```json
{
  "Record": { "...": "..." }
}
```

---

## SAP Document Types

| SAP Type | Used For |
|----------|----------|
| `DR` | Invoice (FINV01), Invoice Reversal, and Debit Note (FDCN01) |
| `DG` | Credit Note (FDCN01) |

Debit Note uses `DR` — same as a regular invoice. Credit Note uses `DG`. Invoice reversals use `DR` with `Cancellation_Flag = "X"`.

---

## Staging Table Field Reference

### Record (Header) Fields

| Field | Max | Format | Notes |
|-------|-----|--------|-------|
| `Invoice_Type` | 3 | `"I"` / `"C"` | `I` = Invoice / Debit Note; `C` = Credit Note |
| `Company_Code` | | `"5130"` | From SAP config; overridden by customer's `company_code` if set |
| `Document_Date` | | `DD.MM.YYYY` | Invoice / document date |
| `Posting_Date` | | `DD.MM.YYYY` | Same as `Document_Date` |
| `Reference_Text` | 16 | | Invoice or DN/CN number — **unique primary field** |
| `Document_Type` | | `"DR"` / `"DG"` | `DR`=Invoice/Debit Note/Reversal, `DG`=Credit Note — **unique primary field** |
| `Cancellation_Flag` | 3 | `""` / `"X"` | `"X"` only for invoice reversals — **unique primary field** |
| `Customer_Code` | 10 | | Customer SAP GL code from `customer_gl_code` |
| `Invoice_Amount` | 13 | `"118000.00"` | Total incl. GST, minus TDS, plus TCS |
| `Currency` | | `"INR"` | Always INR |
| `Business_Place` | | | From SAP config `business_place`; overridden by customer company code |
| `Section_Code` | | | From SAP config `section_code`; overridden by customer company code |
| `Payment_Term` | 4 | | From active SAP config |
| `Baseline_Date` | | `DD.MM.YYYY` | Same as `Document_Date` |
| `Doc_Header_Text` | 25 | | Same as `Reference_Text`; reversal prefixes with `"REV "` |
| `SERVICE_SALE` | | `"S"` (Serv) / `"A"` (Sale) | From service master `is_sale` flag on the first line. `S` = Service, `A` = Sale |
| `IRN_No` | 64 | | e-Invoice IRN — empty until fetched from SAP (auto) |
| `Ack_No` | 20 | | GST acknowledgement number (auto) |
| `IRN_Date` | 10 | `DD.MM.YYYY` | IRN acknowledgement date (auto) |
| `QR_Code` | | | QR code data string returned by SAP/IRP alongside the IRN; empty until IRN is fetched |
| `Nature_of_transaction` | | `"B2B"` / `"B2C"` | `B2B` if customer has GSTIN, else `B2C` |
| `Original_Invoice_No` | | | Present on FDCN01 DN/CN. Value is either the SAP document number or Portbird invoice number — to be confirmed with SAP team |
| `TDS_Amount` | 13 | `"500.00"` / `""` | Header-level total TDS; empty string if zero |
| `TCS_Amount` | 13 | `"100.00"` / `""` | Header-level total TCS; empty string if zero |
| `Item` | | array | One entry per service line |

> **Auto fields** (SAP fills on response): `Processing_Status`, `Fiscal_Year`, `Fiscal_Period`,
> `Push_Date`, `Push_Time`, `Document_Number`, `Message`, `IRN_No`, `Ack_No`, `IRN_Date`, `QR_Code`.

### Item (Line) Fields

| Field | Max | Notes |
|-------|-----|-------|
| `Service_Code` | 10 | GL Account — `service_code` or `gl_code` from the line |
| `Amount` | 13 | Taxable base amount ± sign; always populated (`"0.00"` if zero) |
| `Plant` | | Line → SAP config `plant_code` → company code |
| `Text` | 25 | Service name, truncated to 25 chars |
| `IGST_GL` | 10 | FSTM01 `sap_igst_gl` for this service; blank if not configured |
| `IGST_AMT` | | IGST amount; blank if zero (inter-state only) |
| `CGST_GL` | 10 | FSTM01 `sap_cgst_gl` for this service; blank if not configured |
| `CGST_AMT` | | CGST amount; blank if zero (intra-state only) |
| `SGST_GL` | 10 | FSTM01 `sap_sgst_gl` for this service; blank if not configured |
| `SGST_AMT` | | SGST amount; blank if zero (intra-state only) |
| `UOM` | | From EU01 line item `quantity_uom` → bill line → invoice line |
| `Unit_Price` | | From customer agreement rate, stored in `bill_lines.rate` → invoice line |
| `Quantity` | | From EU01 line item `quantity` (billed qty) → bill line → invoice line |
| `HSN_SAC` | 16 | SAC / HSN code from line |
| `Tax_Amount` | 13 | Total GST per line (CGST + SGST + IGST); to be confirmed total or bifurcated |
| `TDS_GL` | | FSTM01 `sap_tds_gl` for this service; falls back to SAP config `tds_gl` |
| `TDS_Amount` | 13 | TDS deducted on this line; blank if not applicable |
| `TCS_GL` | | FSTM01 `sap_tcs_gl` for this service; falls back to SAP config `tcs_gl` |
| `TCS_Amount` | 13 | TCS collected on this line; blank if not applicable |
| `Round_off_GL` | | From SAP config `round_off_gl`; same for all lines |
| `Rounding_off` | 13 | Rounding adjustment ± sign; empty if zero |
| `Business_Place` | | From line → SAP config `business_place` → company code |
| `Section_Code` | | From line → SAP config `section_code` → company code |
| `Tax_Code` | | Line `sap_tax_code` → SAP config `tax_code` |
| `Profit_Center` | | Line `profit_center` → SAP config `profit_center` |

### GL Account Sources

GL accounts are **not** on the invoice line. They come from two places:

| GL Field | Primary Source | Fallback |
|---|---|---|
| `IGST_GL` | FSTM01 service master `sap_igst_gl` | — (blank if not set) |
| `CGST_GL` | FSTM01 service master `sap_cgst_gl` | — (blank if not set) |
| `SGST_GL` | FSTM01 service master `sap_sgst_gl` | — (blank if not set) |
| `TDS_GL` | FSTM01 service master `sap_tds_gl` | SAP config `tds_gl` |
| `TCS_GL` | FSTM01 service master `sap_tcs_gl` | SAP config `tcs_gl` |
| `Round_off_GL` | SAP config `round_off_gl` | — |
| `Plant` | SAP config `plant_code` | company code |
| `Business_Place` | SAP config `business_place` | company code |
| `Section_Code` | SAP config `section_code` | company code |

**In plain terms:**
- **GST GLs** (IGST/CGST/SGST): set once per service type in FSTM01. If a service has no GL configured, that tax GL is sent blank.
- **TDS/TCS GL**: set per service type in FSTM01 (because different services fall under different Income Tax sections — 194C, 194J, etc.). If not set on the service, the SAP config default is used as a fallback.
- **Round-off GL**: single GL from SAP config, applied to all documents.
- **Plant / Business Place / Section Code**: from SAP config; fall back to company code if not configured.

### Invoice_Amount Calculation

```text
Invoice_Amount = total_amount (from header)
               - TDS
               + TCS
```

If `total_amount` is zero or null, it is computed by summing `line_amount + cgst + sgst + igst` across all lines before applying TDS/TCS.

---

## Scenario 1 — Invoice (FINV01)

**Trigger:** User clicks "Post to SAP" on an approved invoice.  
**Builder:** `sap_builder.build_invoice_payload(invoice_header, invoice_lines)`  
**`Invoice_Type`:** `I`  **`Document_Type`:** `DR`

```json
{
  "Record": {
    "Invoice_Type": "I",
    "Company_Code": "5130",
    "Document_Date": "24.04.2025",
    "Posting_Date": "24.04.2025",
    "Document_Type": "DR",
    "Reference_Text": "dppl/25-26/0001",
    "Cancellation_Flag": "",
    "Customer_Code": "5100001",
    "Invoice_Amount": "9212850.00",
    "Currency": "INR",
    "Business_Place": "5130",
    "Section_Code": "5130",
    "Payment_Term": "Z030",
    "Baseline_Date": "24.04.2025",
    "Doc_Header_Text": "dppl/25-26/0001",
    "SERVICE_SALE": "S",
    "IRN_No": "",
    "Ack_No": "",
    "IRN_Date": "",
    "Nature_of_transaction": "B2B",
    "TDS_Amount": "",
    "TCS_Amount": "",
    "Item": [
      {
        "Service_Code": "OT0051",
        "Amount": "7807500.00",
        "Plant": "5130",
        "Text": "CARGO HANDLING CHARGES",
        "IGST_GL": "",
        "IGST_AMT": "",
        "CGST_GL": "2400101000",
        "CGST_AMT": "702675.00",
        "SGST_GL": "2400102000",
        "SGST_AMT": "702675.00",
        "UOM": "MT",
        "Unit_Price": "173.50",
        "Quantity": "45000",
        "HSN_SAC": "996719",
        "Tax_Amount": "1405350.00",
        "TDS_GL": "2400001000",
        "TDS_Amount": "",
        "TCS_GL": "",
        "TCS_Amount": "",
        "Round_off_GL": "4200000001",
        "Rounding_off": "",
        "Business_Place": "5130",
        "Section_Code": "5130",
        "Tax_Code": "50",
        "Profit_Center": "5130000000"
      }
    ]
  }
}
```

**Status after success:** `invoice_status = 'Posted to SAP'`, `sap_document_number` saved.

---

## Scenario 2 — Invoice Reversal (FINV01 cancellation)

**Trigger:** User cancels an already SAP-posted invoice.  
**Builder:** `sap_builder.build_invoice_reversal_payload(invoice_header, invoice_lines)`  
**`Invoice_Type`:** `I`  **`Cancellation_Flag`:** `"X"`

| Field | Invoice | Reversal |
|-------|---------|----------|
| `Invoice_Type` | `I` | `I` |
| `Document_Type` | `DR` | `DR` |
| `Reference_Text` | `dppl/25-26/0001` | SAP document number or invoice number |
| `Doc_Header_Text` | `dppl/25-26/0001` | `REV 1800000123` |
| `Cancellation_Flag` | `""` | `"X"` |

SAP processes `Cancellation_Flag = X` and posts a reversal entry.

---

## Scenario 3 — Debit Note (FDCN01)

**Trigger:** User clicks "Post to SAP" on an approved debit note.  
**Builder:** `sap_builder.build_fdcn_payload(fdcn_header, fdcn_lines)`  
**`doc_type`:** `DN` → **`Invoice_Type`:** `I`  **`Document_Type`:** `DR`

**Creation types that produce a DN:** `rate_revision`, `manual`

```json
{
  "Record": {
    "Invoice_Type": "I",
    "Company_Code": "5130",
    "Document_Date": "30.03.2026",
    "Posting_Date": "30.03.2026",
    "Document_Type": "DR",
    "Reference_Text": "DN/25-26/0001",
    "Cancellation_Flag": "",
    "Customer_Code": "5100001",
    "Invoice_Amount": "11800.00",
    "Currency": "INR",
    "Business_Place": "5130",
    "Section_Code": "5130",
    "Payment_Term": "Z030",
    "Baseline_Date": "30.03.2026",
    "Doc_Header_Text": "DN/25-26/0001",
    "IRN_No": "",
    "Ack_No": "",
    "IRN_Date": "",
    "Nature_of_transaction": "B2B",
    "Original_Invoice_No": "<SAP doc number or Portbird invoice number>",
    "TDS_Amount": "",
    "TCS_Amount": "",
    "Item": [{ "...": "same structure as Invoice item" }]
  }
}
```

---

## Scenario 4 — Credit Note (FDCN01)

**Trigger:** User clicks "Post to SAP" on an approved credit note.  
**Builder:** `sap_builder.build_fdcn_payload(fdcn_header, fdcn_lines)`  
**`doc_type`:** `CN` → **`Invoice_Type`:** `C`  **`Document_Type`:** `DG`

**Creation types that produce a CN:**

| `creation_type` | Meaning |
|---|---|
| `rate_revision` | Revised rate is lower than original |
| `manual` | User manually created the credit note |
| `eu_deletion` | Auto-created after invoiced EU line deletion |

```json
{
  "Record": {
    "Invoice_Type": "C",
    "Company_Code": "5130",
    "Document_Type": "DG",
    "Reference_Text": "CN/25-26/0001",
    "Cancellation_Flag": "",
    "Original_Invoice_No": "<SAP doc number or Portbird invoice number>",
    "Item": [{ "...": "same structure as Invoice item" }]
  }
}
```

---

## Scenario Matrix

| Document | Module | `doc_type` | `creation_type` | `Invoice_Type` | SAP `Document_Type` | `Cancellation_Flag` | `Original_Invoice_No` |
|----------|--------|------------|-----------------|----------------|---------------------|---------------------|-----------------------|
| Invoice | FINV01 | — | — | `I` | `DR` | `""` | not sent |
| Invoice Reversal *(within 24 hrs)* | FINV01 | — | — | `I` | `DR` | `"X"` | not sent |
| Cancellation CN *(after 24 hrs)* | FDCN01 | `CN` | `cancellation` | `C` | `DG` | `""` | yes |
| Debit Note | FDCN01 | `DN` | `rate_revision` | `I` | `DR` | `""` | yes |
| Debit Note | FDCN01 | `DN` | `manual` | `I` | `DR` | `""` | yes, optional |
| Credit Note | FDCN01 | `CN` | `rate_revision` | `C` | `DG` | `""` | yes |
| Credit Note | FDCN01 | `CN` | `manual` | `C` | `DG` | `""` | yes, optional |
| Credit Note | FDCN01 | `CN` | `eu_deletion` | `C` | `DG` | `""` | yes |

### 24-Hour Cancellation Rule (FB08)

SAP only allows direct reversal (`Cancellation_Flag = "X"`) within **24 hours** of the original SAP posting date. This is the FB08 rule in SAP.

| Time since posting | What happens in Portbird |
|---|---|
| ≤ 24 hours | **Cancel** button shown → posts reversal payload with `Cancellation_Flag = "X"` |
| > 24 hours | **Cancel** button replaced with **Create CN** → creates a full Cancellation Credit Note in FDCN01 which must then be posted to SAP separately |

### What is `Original_Invoice_No`?

`Original_Invoice_No` is the reference of the **invoice being adjusted or reversed**. SAP uses it to link the DN/CN back to the source document in the customer's account.

The exact value to send — SAP document number (e.g. `1800000123`) or Portbird invoice number — **needs to be confirmed with the SAP team** when the PI/PO interface is developed.

| Document | `Original_Invoice_No` contains |
|---|---|
| Debit Note | SAP doc number or Portbird invoice number of the undercharged invoice |
| Credit Note (rate revision) | SAP doc number or Portbird invoice number of the overcharged invoice |
| Cancellation CN | SAP doc number or Portbird invoice number of the invoice being fully reversed |
| Invoice Reversal | Not sent — SAP FB08 uses `Reference_Text` (the SAP doc number) directly |

---

## Company Code Logic

```text
Company_Code:
    If customer has company_code set (inter-company) → customer.company_code
    Else                                              → sap_api_config.company_code

Plant / Business_Place / Section_Code (always our details, never from customer):
    1. Line-level value (if set)
    2. SAP config default (plant_code / business_place / section_code)
```

Customer types checked: `Customer`, `Agent`, `ImporterExporter`.

---

## GST Logic

| Scenario | CGST_GL | CGST_AMT | SGST_GL | SGST_AMT | IGST_GL | IGST_AMT |
|----------|---------|----------|---------|----------|---------|----------|
| Intra-state | populated | populated | populated | populated | `""` | `""` |
| Inter-state | `""` | `""` | `""` | `""` | populated | populated |

SAP does not calculate GST. Portbird sends pre-calculated tax values and their corresponding GL accounts from the service master.

GST GL accounts are configured per service in **FSTM01**:
- `sap_cgst_gl` — CGST liability GL
- `sap_sgst_gl` — SGST liability GL
- `sap_igst_gl` — IGST liability GL

---

## TDS / TCS Logic

TDS and TCS GL accounts follow a 3-level precedence because different services fall under different Income Tax sections (194C, 194J, 194I, etc.), each with its own GL account.

| TDS Section | Nature | Example GL |
|---|---|---|
| 194C | Contractor / Work contracts | `2400001000` |
| 194J | Professional / Technical fees | `2400002000` |
| 194I | Rent | `2400003000` |

**Source (in order):**
1. FSTM01 service master `sap_tds_gl` / `sap_tcs_gl` ← set this per service type
2. SAP config `tds_gl` / `tcs_gl` ← fallback if not set on the service

---

## SERVICE_SALE Flag

Sent at **header level** (not per item). Controls how SAP classifies the document for accounting.

| Value | Label | Meaning | Set when |
|---|---|---|---|
| `"S"` | **Serv** | Service transaction | `Is Sale = No` on service master (default) |
| `"A"` | **Sale** | Sale/goods transaction | `Is Sale = Yes` on service master |

> Note: `"S"` stands for **Serv** (Service), not "Sale". Use `"A"` for Sale to avoid ambiguity.

Derived from the first line's service master `service_sale_flag`. Configured per service in **FSTM01** via the **Is Sale** checkbox.

---

## IRN / e-Invoice Flow

```text
1. Post to SAP
2. SAP PI forwards to Cygnet for IRN generation
3. IRN is not returned in the initial POST response
4. SAP returns IRN_No, Ack_No, IRN_Date
5. Portbird saves gst_irn, gst_ack_number, gst_ack_date
6. Re-posted payloads include IRN fields if already available
```

---

## Error Handling

| Condition | Behavior |
|-----------|----------|
| No active SAP config | Return error and log to `integration_logs` |
| OAuth token failure | Return error and log |
| HTTP 4xx/5xx from SAP | Log full request and response; set document to SAP failed |
| Network timeout | Return error and log |
| Successful post | Save `sap_document_number` and update document status |

All attempts are written to `integration_logs` with:
- `integration_type`: `SAP` or `SAP_IRN_FETCH`
- `source_type`: `Invoice`, `CreditNote`, `DebitNote`
- `source_id`, `source_reference`, `request_body`, `response_body`, `status`, `created_by`

---

## Source Field Mapping

### FINV01 Invoice (`invoice_header`)

| Payload Field | DB Column |
|---|---|
| `Reference_Text` | `invoice_number` |
| `Document_Date` | `invoice_date` |
| `Customer_Code` | `customer_gl_code` |
| `Invoice_Amount` | `total_amount` |
| `IRN_No` | `irn` |
| `Ack_No` | `ack_number` |
| `IRN_Date` | `irn_date` |
| `SERVICE_SALE` | service master `service_sale_flag` of first line → `"S"` (Serv) or `"A"` (Sale) |

### FDCN01 DN / CN (`fdcn_header`)

| Payload Field | DB Column |
|---|---|
| `Reference_Text` | `doc_number` |
| `Document_Date` | `doc_date` |
| `Customer_Code` | `customer_gl_code` |
| `Invoice_Amount` | `total_amount` |
| `Original_Invoice_No` | `original_invoice_number` |
| `IRN_No` | `gst_irn` |
| `Ack_No` | `gst_ack_number` |
| `IRN_Date` | `gst_ack_date` |
| `SERVICE_SALE` | service master `service_sale_flag` of first line → `"S"` (Serv) or `"A"` (Sale) |

### Service Line (`invoice_lines` / `fdcn_lines`)

| Payload Field | DB Column / Source |
|---|---|
| `Service_Code` | `service_code` or `gl_code` |
| `Amount` | `line_amount` |
| `CGST_AMT` | `cgst_amount` |
| `SGST_AMT` | `sgst_amount` |
| `IGST_AMT` | `igst_amount` |
| `Tax_Amount` | `cgst_amount + sgst_amount + igst_amount` |
| `Text` | `service_name` (max 25 chars) |
| `UOM` | `invoice_lines.uom` ← `bill_lines.uom` ← `lueu_lines.quantity_uom` (EU01 line item) |
| `Unit_Price` | `invoice_lines.rate` ← `bill_lines.rate` ← customer agreement rate (fetched in FIN01 billing) |
| `Quantity` | `invoice_lines.quantity` ← `bill_lines.quantity` ← `lueu_lines.quantity` (EU01 line item) |
| `HSN_SAC` | `invoice_lines.sac_code` |
| `IGST_GL` | service master `sap_igst_gl` |
| `CGST_GL` | service master `sap_cgst_gl` |
| `SGST_GL` | service master `sap_sgst_gl` |
| `TDS_GL` | service master `sap_tds_gl` → config `tds_gl` |
| `TCS_GL` | service master `sap_tcs_gl` → config `tcs_gl` |
| `Round_off_GL` | config `round_off_gl` |
| `TDS_Amount` | `tds_amount` |
| `TCS_Amount` | `tcs_amount` |
| `Rounding_off` | `rounding_off` |
| `Tax_Code` | `sap_tax_code` → config `tax_code` |
| `Profit_Center` | `profit_center` → config `profit_center` |
