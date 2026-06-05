# Carrum Finance Reconciliation Tool
**Carrum Mobility · Finance**

Matches Carrum portal transactions against Cashfree and Mumbai Black UTR reports.
Auto-approves verified matches and lets the team manually approve the rest — all from one screen.

---

## Folder Setup

```
cashfree-recon/
├── cashfree_recon.py     ← main app
├── requirements.txt      ← dependencies
└── README.md             ← this file
```

---

## First-Time Setup

```bash
# 1. Create folder
mkdir cashfree-recon && cd cashfree-recon

# 2. Virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate        # Mac / Linux
venv\Scripts\activate           # Windows

# 3. Install
pip install -r requirements.txt

# 4. Run
python3 -m streamlit run cashfree_recon.py
```

Opens at `http://localhost:8501`.

---

## How to Use

| Step | Action |
|------|--------|
| **Sidebar** | Paste JWT token · set remarks · pick Tally voucher date |
| **Step 1** | Upload Carrum CSV (required) + Cashfree XLSX + Mumbai Black XLSX (both optional) |
| **Step 2** | Click **Run Reconciliation** |
| **Step 3** | Review three tabs · tick rows in Unmatched / Manual Review to add to approval |
| **Step 4** | Confirm + click **Approve** |

---

## Result Buckets

| Bucket | Meaning | Action |
|--------|---------|--------|
| ✅ Matched | UTR + Amount verified (Cashfree or Mumbai Black) | Auto-included in approval |
| ⚠️ Unmatched | Valid UTR not found in any report (EDC / other city) | Tick to include in approval |
| ❓ Manual Review | Blank or text UTR (referral notes, towing charges) | Tick to include in approval |

**Guarantee:** Matched + Unmatched + Manual Review = 100 % of in-scope rows.

---

## Matching Rules

**Carrum filter:** `payment_mode = other` · `approval_status = pending` · `payment_status = success`

| Source | Carrum key | Report key | Amount |
|--------|-----------|------------|--------|
| Cashfree | `UTR Number` | `Bank Reference No.` | Exact |
| Mumbai Black | `UTR Number` | `UTR` | Exact |

> `UTR No.` in Cashfree (e.g. `CB0133840860`) is a batch settlement ID — not used.  
> The real key is `Bank Reference No.`

---

## Tally Import CSV

Generated automatically for **Cashfree-matched rows only**.  
Two rows per transaction: Dr `Cashfree Settlement` / Cr `DRIVERID_SD`

```
Voucher Date | Journal | UTR | Cashfree Settlement | Amount | Dr. | UTR | Serial
Voucher Date | Journal | UTR | AAAA0001_SD         | Amount | Cr. | UTR | Serial
```

Driver ID format: `Driver Small ID` from Carrum + `_SD` (e.g. `AAAO0886_SD`)

---

## API

- **Endpoint:** `POST https://dev.carrum.co.in/api//v1/payment/review`
- **Auth:** `Authorization: carrum <JWT>`
- **Payload:** Array of `{ id, status, review_remarks }`
- **Single call** — auto-matched rows use `auto_rem`, manually selected rows use `man_rem`

JWT expires periodically. If API returns **401**, paste a fresh token in the sidebar.

---

## Files

| File | Source | Format |
|------|--------|--------|
| Carrum export | Carrum portal | `.csv` |
| Cashfree report | Cashfree dashboard (3-day) | `.xlsx` |
| Mumbai Black UTR | Mumbai Black hub report | `.xlsx` |
