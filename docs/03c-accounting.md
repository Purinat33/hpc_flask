# Accounting & Compliance

> **Disclaimer**: This section explains the accounting logic implemented in the app for internal control and export purposes. It is **not legal or tax advice**. Policies should be reviewed by your finance team/auditor.

This chapter covers **revenue recognition** (over‑time services), **VAT handling (Thailand)**, the **simplified ECL** model for receivables/contract assets, and how these policies map to our **journal → posted GL → exports**.

---

## 1) Design goals

- Correctly recognise service **revenue over time** (service month) with a **contract asset** until invoicing.
- Keep **VAT** outside revenue and recognise it on **tax invoice issuance**.
- Maintain a lightweight, auditable **GL** suitable for CSV export to external systems (e.g., Xero mappings).
- Support **ECL** (expected credit loss) using a simplified, period‑end allowance with an ageing matrix.
- Preserve integrity via an **HMAC‑chained audit log** and signed **export manifests**.

---

## 2) Chart of accounts (defaults)

| ID   | Name                                  | Type           |
| ---- | ------------------------------------- | -------------- |
| 1000 | Cash/Bank                             | ASSET          |
| 1100 | Accounts Receivable                   | ASSET          |
| 1150 | Contract Asset (Unbilled A/R)         | ASSET          |
| 1159 | Allowance for ECL – Contract assets   | ASSET (contra) |
| 1190 | Allowance for ECL – Trade receivables | ASSET (contra) |
| 2000 | Unearned Revenue _(reserved)_         | LIABILITY      |
| 2100 | VAT Output Payable                    | LIABILITY      |
| 3000 | Retained Earnings                     | EQUITY         |
| 4000 | Service Revenue                       | INCOME         |
| 5000 | Cost of Service _(reserved)_          | EXPENSE        |
| 6100 | Impairment loss (ECL)                 | EXPENSE        |

These IDs map 1‑to‑1 to the posting rules below and to export columns. You may adapt names/IDs in code if your chart differs.

---

## 3) Revenue recognition (over‑time; service month)

The platform treats HPC compute as an **over‑time service**. Revenue is recognised in the month services are rendered. Billing then reclassifies the contract asset into receivable and VAT.

### Journal sequence (per receipt)

1. **Service period end (accrual)**
   _Dr 1150 Contract Asset_ / _Cr 4000 Service Revenue_ _(net of VAT)_
2. **Invoice issued**
   _Dr 1100 Accounts Receivable_ (gross) / _Cr 1150 Contract Asset_ (net) / _Cr 2100 VAT Output_ (VAT)
3. **Cash received** _(if paid)_
   _Dr 1000 Cash/Bank_ / _Cr 1100 Accounts Receivable_

### VAT netting

For step 1 we split `total` into `net + VAT` using the configured rate and **book only the net** as revenue/contract asset. VAT is never part of revenue.

---

## 4) VAT (Thailand) — application

- **Recognition point**: Output VAT arises on issuance of a valid **tax invoice** for taxable supplies.
- **Presentation**: VAT is presented **separately** from revenue in invoices and exports.
- **Invoice content**: must meet the **full tax invoice** content requirements.
- **Rates/labels**: Configured via env; the system can compute inclusive or additive VAT totals; journals always treat VAT as liability.

---

## 5) ECL (IFRS 9 simplified approach)

We use the **simplified approach** for trade receivables and **contract assets**:

- Measure **lifetime ECL** using a period‑end provisioning matrix by ageing bucket.
- Post **only the change (delta)** in allowance each month:
  _Dr 6100 Impairment loss (ECL)_ / _Cr 1190_ (A/R) and/or _Cr 1159_ (contract assets).
- ECL does **not** affect revenue timing; it affects **net carrying amounts** of receivables/contract assets.

> Operational note: The demo UI shows the allowance effect in the **Income Statement** and balances in **Trial Balance**; detailed matrices are out of scope for this release, but can be imported or parameterised if your policy requires.

---

## 6) Periods, close/reopen & controls

- **Accounting periods** (YYYY/MM) track **open** or **closed** states. Closing a period prevents further mutation of posted entries and enables export tagging.
- A **preview journal** is derived on demand from receipts (for any date window) and shown as _Preview (derived)_.
- **Posting to GL** writes immutable entries grouped by **Journal Batches**: `accrual`, `issue`, `payment`, `close`, `reopen`.
- **Reopen** reverses the `close` batch and returns the period to `open` for corrective postings.
- All admin actions write to a **tamper‑evident audit log** (HMAC chain) with verifier endpoints.

---

## 7) Exports & integrity

- **CSV exports**: posted GL can be exported as CSV; convenience endpoints also produce **Xero Sales** and **Xero Bank** CSVs.
- **Formal GL ZIP**: bundles CSV + a JSON **manifest** and **HMAC signature** (with key id). Finance can verify the bundle without DB access.
- Export runs are recorded with IDs and include the list of **batch IDs** captured in the package.

---

## 8) How UI maps to accounting

- **Ledger page** shows (a) Derived Journal (Preview) or (b) Posted GL (Authoritative), switchable via the mode toggle.
- **Income Statement** and **Balance Sheet** are computed from the same journal: TB → P&L and TB → BS.
- **Receipts** drive accrual/issue/payment postings; **status** and **dates** determine which of the three appear.

---

## 9) Worked example

A monthly receipt for ฿10,700 total with 7% VAT (inclusive):

- Split: net ฿10,000; VAT ฿700.
- **Service month**: Dr 1150 10,000 / Cr 4000 10,000.
- **Invoice**: Dr 1100 10,700 / Cr 1150 10,000 / Cr 2100 700.
- **Cash** (later): Dr 1000 10,700 / Cr 1100 10,700.

> If marked paid in a later month, the cash entry lands in that month while revenue stays in the service month.

---

## 10) Implementation hooks (where to look in code)

- Chart of accounts & journal builders: `services/accounting.py`
  – `_entry_service_delivery`, `_entry_receipt_issue`, `_entry_receipt_paid`, `derive_journal`, `trial_balance`, `income_statement`, `balance_sheet`.
- Periods, batches & posted GL: `models/gl.py`; posting logic: `services/gl_posting.py`.
- Exports & signing: `services/accounting_export.py`.
- VAT split & tax config: `_split_vat()` and `_tax_cfg()`.
- Audit chain & verification: `models/audit_store.py` (HMAC, key rotation fields).

---

## References (same style as the ledger footer)

- **IFRS 15** — Revenue from Contracts with Customers — §§ 35, 38, 105–107.
- **IFRS 9** — Financial Instruments — § 5.5.15; App B § B5.5.35.
- **TFRS 15** — มาตรฐานการรายงานทางการเงิน ฉบับที่ 15 (รายได้จากสัญญาที่ทำกับลูกค้า)
- **TFRS 9** — มาตรฐานการรายงานทางการเงิน ฉบับที่ 9 (เครื่องมือทางการเงิน)
- **VAT — ประมวลรัษฎากร** — มาตรา 77/1 (นิยามภาษีมูลค่าเพิ่ม), มาตรา 86/4 (ข้อกำหนด “ใบกำกับภาษี” แบบเต็ม); คำสั่งกรมสรรพากรที่ ป.86/2542 (การออกใบกำกับภาษีหลายแผ่น/ข้อกำหนดรูปแบบ).
