# PORTMAN → SAP: Invoice Payload Structure Guide

Last updated: 2026-07-20
Audience: anyone integrating with or supporting the PMS ↔ SAP (PORTBIRD / DynaportInvoice) interface.

This guide covers:

1. How the outbound payload is structured (envelope, header, line items)
2. How to structure each tax scenario — CGST+SGST, IGST, No GST, TDS, TCS, Round-off
3. The immediate SAP response (staging acknowledgement)
4. The asynchronous SAP callback (Document Number / IRN / QR code) and the response PMS sends back
5. Important note on e-invoicing: GST is reported against the **PMS invoice number**, not the SAP ODN number

---

## 1. Transport

| | |
| --- | --- |
| Endpoint | `POST {base_url}/RESTAdapter/DynaportInvoice` |
| Auth | OAuth2 `client_credentials` → `POST {base_url}/RESTAdapter/OAuthServer` (credentials as **query parameters**), then `Authorization: Bearer <token>` |
| Content type | `application/json` |
| Token validity | ~3600 s (PMS caches and refreshes with a 60 s buffer) |

Every request/response is logged in `integration_logs` (viewable in FSAP01/FLOG01). Outbound posting is queued (`sap_outbound_queue`) with up to 10 automatic retries, 5 minutes apart.

## 2. Payload Envelope

```json
{
  "Record_Header": [
    {
      "...header fields...": "",
      "ITEM": [ { "...line item fields...": "" } ]
    }
  ]
}
```

**Formatting rules (apply everywhere):**

- Every value is a JSON **string** — including amounts and quantities.
- Dates are `DD.MM.YYYY`.
- Amounts use 2 decimals (`"3276306.46"`). Quantity uses 3 decimals (`"15915.000"`).
- Optional amounts (GST, TDS, TCS, round-off) are sent as an **empty string `""` when zero** — never `"0.00"`, never `null`.
- A GL field is sent only when its matching amount is non-zero; otherwise it is `""`.

## 3. Header Fields (`Record_Header[0]`)

| Field | Max | Value | Notes |
| --- | --- | --- | --- |
| `Invoice_Credit` | 1 | `I` or `C` | `I` = Invoice / Debit Note / Reversal-of-invoice; `C` = Credit Note |
| `Company_code` | 4 | e.g. `5130` | Customer master `company_code` override, else active SAP config default |
| `Invoice_date` | 10 | `17.07.2026` | PMS invoice date |
| `Posting_Date` | 10 | `17.07.2026` | Same as `Invoice_date` |
| `Reference` | 16 | `DPPL/26-27/166` | **PMS document number.** SAP matches everything back on this — see §7 |
| `Document_type` | 2 | `DR` / `DG` | `DR` = Invoice & Debit Note, `DG` = Credit Note |
| `Customer_Code` | 10 | `I510257` | SAP customer code from the party master |
| `Invoice_Amount` | 13 | `3276306.46` | **taxable + GST + TDS − TCS + round-off**, always positive. Built from the header's `subtotal`/`cgst`/`sgst`/`igst`, never from `total_amount` — that is a display total and already includes TCS. |
| `Business_place` | 4 | `5130` | SAP config; defaults to company code |
| `Section_code` | 4 | `5130` | SAP config; defaults to company code |
| `Text` | 25 | invoice number | Short narration |
| `Document_Header_Text` | 25 | invoice number | |
| `Payment_Term` | 4 | | From SAP config; may be blank |
| `Credit_Control_Area` | 4 | `5130` | SAP config; defaults to company code |
| `Cancellation_Flag` | 1 | `""` or `X` | `X` only for reversals (see §6) |
| `Nature_of_transaction` | 3 | `B2B` / `B2C` | `B2B` when the customer has a GSTIN, else `B2C` |
| `Service_Sale` | 1 | `S` / `A` | `S` = Service, `A` = Sale (from FSTM01 service master of the first line) |
| `Currency` | 3 | `INR` | |
| `Payment_term` | 4 | | Duplicate of `Payment_Term` — required by the PORTBIRD spec, keep both |
| `Baseline_Date` | 10 | `17.07.2026` | Same as `Invoice_date` |

## 4. Line Item Fields (`ITEM[]`)

| Field | Max | Notes |
| --- | --- | --- |
| `Reference` | 16 | Mirrors the header `Reference` on every item |
| `GL_account` | 10 | Revenue GL from FSTM01 service master (`sap_gl_account`). Never an HSN/SAC code |
| `Amount` | 13 | **Taxable** line amount (GST excluded). Always present, `"0.00"` minimum |
| `Tax_Code` | 2 | Depends on tax scenario — see §5. Blank when the line has no GST |
| `Cost_Center` | 10 | Line → service master → blank |
| `Plant` | 4 | Line value or SAP config `plant_code` |
| `Text` | 25 | Service name |
| `Profit_Center` | 10 | Line → service master → SAP config |
| `HSN_SAC` | 16 | SAC/HSN code of the service |
| `CGST_AMT` / `SGST_AMT` / `IGST_AMT` | 13 | Per scenario, blank when zero |
| `IGST_GL` / `SGST_GL` / `CGST_GL` | 10 | Per scenario, from service master — see §5 |
| `UOM` | 3 | e.g. `MT` |
| `Unit_Price` | 13 | Blank if unknown or if merged lines had mixed rates (`Amount` is authoritative) |
| `Quantity` | 13 | 3 decimals |
| `TDS_GL` / `TDS_amount` | 10 / 13 | Only when TDS applies — see §5.4 |
| `TCS_GL` / `TCS_amount` | 10 / 13 | Only when TCS applies — see §5.5 |
| `Round_off_GL` / `Round_off_Value` | 10 / 13 | **First item only** — see §5.6 |

**One ITEM per GL account.** Lines posting to the same GL with identical tax code, HSN, GST/TDS/TCS GLs, centers, plant, text and UOM are merged into a single item: `Amount`, GST amounts, TDS/TCS and `Quantity` are summed. `Unit_Price` survives the merge only when the rate is uniform; mixed rates leave it blank.

## 5. Tax Scenarios — How to Structure Each Type

The GST fields follow one rule set, verified against SAP-accepted postings:

| Scenario | Tax_Code | CGST_AMT / SGST_AMT | IGST_AMT | CGST_GL / SGST_GL | IGST_GL |
| --- | --- | --- | --- | --- | --- |
| **Intra-state (CGST+SGST)** | CGST tax code (e.g. `60`) | filled | `""` | filled | **filled** (all three GLs sent) |
| **Inter-state (IGST)** | IGST tax code (e.g. `62`) | `""` | filled | `""` (must be blank) | filled |
| **No GST** | `""` | `""` | `""` | `""` | `""` |

Note the asymmetry: intra-state sends **all three** GST GLs (including `IGST_GL`); inter-state sends **only** `IGST_GL` and must leave the CGST/SGST GLs blank.

### 5.1 Intra-state — CGST + SGST

Customer GSTIN state = supplier state. 9% + 9% on the taxable amount.

```json
{
  "Record_Header": [
    {
      "Invoice_Credit": "I",
      "Company_code": "5130",
      "Invoice_date": "17.07.2026",
      "Posting_Date": "17.07.2026",
      "Reference": "DPPL/26-27/166",
      "Document_type": "DR",
      "Customer_Code": "I510257",
      "Invoice_Amount": "3276306.46",
      "Business_place": "5130",
      "Section_code": "5130",
      "Text": "DPPL/26-27/166",
      "Document_Header_Text": "DPPL/26-27/166",
      "Payment_Term": "",
      "Credit_Control_Area": "5130",
      "Cancellation_Flag": "",
      "Nature_of_transaction": "B2B",
      "Service_Sale": "S",
      "Currency": "INR",
      "Payment_term": "",
      "Baseline_Date": "17.07.2026",
      "ITEM": [
        {
          "Reference": "DPPL/26-27/166",
          "GL_account": "4101076010",
          "Amount": "2776530.90",
          "Tax_Code": "60",
          "Cost_Center": "",
          "Plant": "5130",
          "Text": "Cargo Handling Unloading",
          "Profit_Center": "510302",
          "HSN_SAC": "996719",
          "CGST_AMT": "249887.78",
          "SGST_AMT": "249887.78",
          "IGST_AMT": "",
          "IGST_GL": "1404051142",
          "SGST_GL": "1404051141",
          "CGST_GL": "1404051140",
          "UOM": "MT",
          "Unit_Price": "174.46",
          "Quantity": "15915.000",
          "TDS_GL": "",
          "TDS_amount": "",
          "TCS_GL": "",
          "TCS_amount": "",
          "Round_off_GL": "",
          "Round_off_Value": ""
        }
      ]
    }
  ]
}
```

`Invoice_Amount` = 2,776,530.90 + 249,887.78 + 249,887.78 = **3,276,306.46**.

### 5.2 Inter-state — IGST

Same invoice if the customer were in another state: 18% IGST instead of 9%+9%. Only the fields below differ from §5.1 — CGST/SGST amounts **and GLs** go blank:

```json
{
  "Tax_Code": "62",
  "CGST_AMT": "",
  "SGST_AMT": "",
  "IGST_AMT": "499775.56",
  "IGST_GL": "1404051142",
  "SGST_GL": "",
  "CGST_GL": ""
}
```

Header `Invoice_Amount` = 2,776,530.90 + 499,775.56 = `"3276306.46"`.

### 5.3 No GST (exempt / zero-rated line)

All GST fields blank, including `Tax_Code`:

```json
{
  "Tax_Code": "",
  "CGST_AMT": "",
  "SGST_AMT": "",
  "IGST_AMT": "",
  "IGST_GL": "",
  "SGST_GL": "",
  "CGST_GL": ""
}
```

### 5.4 TDS

When TDS applies on the line, fill `TDS_GL` (service master `sap_tds_gl`, fallback SAP config `tds_gl`) and `TDS_amount`. Everything else follows the normal GST rules for the line.

Example — taxable 200,000.00, CGST/SGST 18,000.00 each, TDS 4,000.00:

```json
{
  "Reference": "DPPL/26-27/170",
  "GL_account": "4201090080",
  "Amount": "200000.00",
  "Tax_Code": "60",
  "CGST_AMT": "18000.00",
  "SGST_AMT": "18000.00",
  "IGST_AMT": "",
  "IGST_GL": "1404051142",
  "SGST_GL": "1404051141",
  "CGST_GL": "1404051140",
  "TDS_GL": "2206560017",
  "TDS_amount": "4000.00",
  "TCS_GL": "",
  "TCS_amount": ""
}
```

Header `Invoice_Amount` = 200,000 + 18,000 + 18,000 **+ 4,000** = `"240000.00"` (TDS is **added** in the header total).

### 5.5 TCS

Typically on sale-type transactions (e.g. scrap — header `Service_Sale: "A"`). Fill `TCS_GL` (service master `sap_tcs_gl`, fallback SAP config `tcs_gl`) and `TCS_amount`:

```json
{
  "Reference": "DPPL/26-27/171",
  "GL_account": "4201090080",
  "Amount": "200000.00",
  "Tax_Code": "60",
  "CGST_AMT": "18000.00",
  "SGST_AMT": "18000.00",
  "TDS_GL": "",
  "TDS_amount": "",
  "TCS_GL": "2206560025",
  "TCS_amount": "2360.00"
}
```

Header `Invoice_Amount` = 200,000 + 18,000 + 18,000 **− 2,360** = `"233640.00"` (TCS is **subtracted** in the header total).

### 5.6 Round-off

Round-off is a **header-level** value but is carried on the **first ITEM only** (all other items keep both fields blank). The GL comes from SAP config (`round_off_gl`). The value is positive when the invoice gross was rounded **up** (SAP-validated behaviour).

Example — taxable 1,234.56 + CGST 111.11 + SGST 111.11 = 1,456.78, rounded to 1,457.00:

```json
{
  "Reference": "DPPL/26-27/172",
  "GL_account": "4101076010",
  "Amount": "1234.56",
  "CGST_AMT": "111.11",
  "SGST_AMT": "111.11",
  "Round_off_GL": "5501260001",
  "Round_off_Value": "0.22"
}
```

Header `Invoice_Amount` = `"1457.00"` (round-off included).

All of the above combine freely on one line — e.g. an IGST line with TDS and round-off populates the IGST set (§5.2), the TDS pair (§5.4) and the round-off pair (§5.6) together.

## 6. Document Types & Reversals

| Case | `Invoice_Credit` | `Document_type` | `Cancellation_Flag` | `Reference` |
| --- | --- | --- | --- | --- |
| Invoice | `I` | `DR` | `""` | PMS invoice number |
| Debit Note | `I` | `DR` | `""` | **Original invoice number** when raised against an invoice; else the DN number |
| Credit Note | `C` | `DG` | `""` | **Original invoice number** when raised against an invoice; else the CN number |
| Invoice reversal (FB08, ≤ 24 h from posting) | `I` | `DR` | `X` | Original PMS invoice number |
| Cancellation after 24 h | `C` | `DG` | `""` | Original PMS invoice number (posted as a Credit Note) |

- A **reversal** payload is byte-identical to the original invoice payload except `Cancellation_Flag: "X"`. SAP finds the original posted document by `Reference` — do not send the SAP document number.
- A CN/DN raised **against** an invoice carries the original invoice's number in header `Reference`, `Text`, `Document_Header_Text` **and** every item `Reference`; only `Document_type`/`Invoice_Credit` distinguish it from the invoice.
- After the 24-hour FB08 window, cancellation is posted as a Credit Note against the invoice (same shape as the invoice, `C`/`DG`, flag blank).

## 7. Immediate SAP Response (Staging Acknowledgement)

The synchronous response only confirms the document entered SAP's **staging table** — it is *not* the posting result:

```json
{
  "Record": [
    {
      "Invoice": "DPPL/26-27/166",
      "Status": "N",
      "Message": "Invoice saved in staging table"
    }
  ]
}
```

`Status: "N"` = new/staged. The actual posting result (SAP document number, IRN, QR code) arrives later via the callback (§8). On this ack, PMS marks the invoice *Posted to SAP* and waits.

## 8. SAP → PMS Callback (Posting Result + e-Invoice Details)

After SAP processes the staged document, SAP calls back:

```
POST https://<pms-host>/api/sap/callback
Authorization: Bearer <token issued from the PMS admin panel>
Content-Type: application/json
```

Multiple documents can be batched in a single call:

```json
{
  "Record": [
    {
      "Reference": "DPPL/26-27/159",
      "Document_Number": "2100000029",
      "Posting_Date": "10.07.2026",
      "Company_Code": "5130",
      "Message": "Document Posted successfully",
      "IRN_No": "02c1ecc1d67142a7064a09bb4e1ba4db4ec7f3a7198301ad55949f6852b5398a",
      "Ack_No": "122633554569803",
      "IRN_Date": "11.07.2026",
      "QR_Code": "iVBORw0KGgo...  (base64 PNG of the signed e-invoice QR)"
    }
  ]
}
```

| Callback field | Meaning / handling in PMS |
| --- | --- |
| `Reference` | The PMS document number sent in the outbound payload. Matched to `invoice_header.invoice_number` first, then `fdcn_header.doc_number` |
| `Document_Number` | SAP accounting document number → stored as `sap_document_number` |
| `Posting_Date` | Accepted as `dd.mm.yyyy` or `yyyy-mm-dd`; SAP null dates (`00.00.0000`) are ignored |
| `Message` | Free text. If it contains *error* / *invalid* / *fail*, the document is marked **SAP Failed** with the message stored |
| `IRN_No`, `Ack_No`, `IRN_Date` | e-invoice registration details → stored; invoice status becomes **Posted to GST** when an IRN is present |
| `QR_Code` | Base64 PNG, printed on the final invoice |

Behaviour guarantees:

- **Idempotent** — resending the same IRN for a document is a no-op (`"Already recorded"`).
- **IRN mismatch protection** — a different IRN for an already-registered document is rejected (`Status: "E"`).
- **Reversal detection** — a callback carrying a *different* SAP document number (and no IRN) for an already-posted invoice records it as the reversal document and marks the invoice *Cancelled*.
- Unknown `Reference` values are accepted (`"Reference not found - accepted"`) so one bad record never fails the batch.

PMS always answers HTTP 200 with a per-record result:

```json
{
  "Record": [
    { "Reference": "DPPL/26-27/159", "Status": "S", "Message": "Updated" },
    { "Reference": "DPPL/26-27/160", "Status": "S", "Message": "Updated" },
    { "Reference": "DPPL/26-27/161", "Status": "S", "Message": "Updated" }
  ]
}
```

`Status` is `S` (applied) or `E` (that record failed — check `Message`). Every authenticated callback is logged and visible in the SAP Inbound log viewer.

## 9. ⚠️ e-Invoice / GST Number — Important

**The invoice number reported to the GST portal (IRP) must be the PMS invoice number** — the value sent as `Reference` (e.g. `DPPL/26-27/166`) — **not the SAP ODN / SAP document number**.

Generating the IRN against the correct number is **SAP's responsibility** — PMS only supplies `Reference` in the outbound payload and stores what the callback returns. It is called out here because an e-invoice registered under the SAP ODN instead of the PMS invoice number would not match the physical invoice issued to the customer.

## 10. Where the Values Come From (PMS side)

| Source | Provides |
| --- | --- |
| **FSTM01** service master | `GL_account`, GST GLs (`sap_igst_gl`/`sap_cgst_gl`/`sap_sgst_gl`), `sap_tds_gl`, `sap_tcs_gl`, profit/cost center, `Service_Sale` flag, UOM |
| **SAPCFG** active config | Company code default, business place, section code, credit control area, plant, CGST/IGST tax codes, TDS/TCS/round-off GL fallbacks, payment term, endpoints & OAuth credentials |
| Customer/Agent/Importer master | `Customer_Code` (SAP customer code), company-code override, GSTIN (drives B2B/B2C) |
| Invoice / DN / CN document | Dates, reference number, line amounts, GST amounts, TDS/TCS, round-off, quantities |

Implementation: `sap_builder.py` (payload construction), `sap_client.py` (OAuth + POST), `sap_queue.py` (async retry queue), `sap_inbound.py` (callback endpoint). Regression tests for every tax-scenario rule: `test_sap_builder.py`.
