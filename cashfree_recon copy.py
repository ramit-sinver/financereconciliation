"""
Carrum Mobility — Finance Reconciliation Tool
Matches Carrum & Uber Black transactions against Cashfree, then approves Carrum matches via API.
"""

import io
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

# ─── Page config ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Carrum · Finance Recon",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .recon-badge-green  { background:#d4edda; color:#155724; padding:3px 10px; border-radius:12px; font-size:13px; font-weight:600; }
    .recon-badge-amber  { background:#fff3cd; color:#856404; padding:3px 10px; border-radius:12px; font-size:13px; font-weight:600; }
    .recon-badge-red    { background:#f8d7da; color:#721c24; padding:3px 10px; border-radius:12px; font-size:13px; font-weight:600; }
    div[data-testid="stMetricValue"] { font-size: 2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ─── Constants ────────────────────────────────────────────────────────────────

API_URL = "https://api.carrum.co.in/api//v1/payment/review"

CARRUM_REQUIRED = {
    "Payment Mode", "Approval Status", "Payment Status",
    "UTR Number", "Amount", "Transaction ID",
}
CF_REQUIRED = {"Transaction Status", "Bank Reference No.", "Amount"}
UB_REQUIRED = {"UTR", "Amount"}

DISPLAY_COLS = [
    "Data_Source", "Transaction ID", "UTR Number", "Driver Name",
    "Driver Small ID", "Amount", "Hub",
    "Transaction Date Date", "DM Name",
]

# ─── Core logic ───────────────────────────────────────────────────────────────

def parse_utr(val) -> tuple:
    """Returns (clean_str: str, is_numeric: bool)."""
    if pd.isna(val) or str(val).strip() == "":
        return ("", False)
    s = str(val).strip()
    if "." in s:
        s = s.split(".")[0]
    try:
        int(s)
        return (s, True)
    except ValueError:
        return (s, False)


def reconcile(carrum_df: pd.DataFrame = None, ub_df: pd.DataFrame = None, cf_df: pd.DataFrame = None) -> dict:
    """Reconciliation pipeline (Dual Primary Sources vs Cashfree)."""
    
    primary_list = []
    c_count = 0
    ub_count = 0

    # ── 1. Standardize Carrum Data ──
    if carrum_df is not None and not carrum_df.empty:
        scope_c = carrum_df[
            (carrum_df["Payment Mode"].astype(str).str.strip().str.lower() == "other") &
            (carrum_df["Approval Status"].astype(str).str.strip().str.lower() == "pending") &
            (carrum_df["Payment Status"].astype(str).str.strip().str.lower() == "success")
        ].copy()
        scope_c["_utr_raw"] = scope_c["UTR Number"]
        scope_c["_amount_raw"] = scope_c["Amount"]
        scope_c["Data_Source"] = "Carrum"
        c_count = len(scope_c)
        primary_list.append(scope_c)

    # ── 2. Standardize Uber Black Data ──
    if ub_df is not None and not ub_df.empty:
        scope_ub = ub_df.copy()
        scope_ub["_utr_raw"] = scope_ub["UTR"]
        scope_ub["_amount_raw"] = scope_ub["Amount"]
        # Map back to standardized display columns
        scope_ub["UTR Number"] = scope_ub["UTR"] 
        scope_ub["Data_Source"] = "Uber Black"
        ub_count = len(scope_ub)
        primary_list.append(scope_ub)

    if not primary_list:
        return _empty_result(0, 0, len(cf_df) if cf_df is not None else 0)

    # Combine all primary rows to reconcile
    primary = pd.concat(primary_list, ignore_index=True)

    # ── 3. Classify UTRs ──
    parsed = primary["_utr_raw"].apply(parse_utr)
    primary["_utr_str"]   = parsed.apply(lambda x: x[0])
    primary["_utr_valid"] = parsed.apply(lambda x: x[1])

    manual_review = primary[~primary["_utr_valid"]].copy()
    manual_review["Recon_Status"] = "Manual Review"
    manual_review["Recon_Reason"] = manual_review["_utr_str"].apply(
        lambda x: "Blank UTR" if x == "" else "Non-numeric / text UTR"
    )

    remaining = primary[primary["_utr_valid"]].copy()
    
    matched_list = []
    unmatched_list = []
    cf_dup_count = 0

    # ── 4. Cashfree Match ──
    if cf_df is not None and not cf_df.empty:
        cf_ok = cf_df[
            (cf_df["Transaction Status"].astype(str).str.strip().str.upper() == "SUCCESS") &
            (pd.to_numeric(cf_df["Amount"], errors="coerce").fillna(0) > 0)
        ].copy()

        cf_ok["_bank_ref"] = cf_ok["Bank Reference No."].astype(str).str.strip().apply(
            lambda x: x.split(".")[0] if "." in x else x
        )
        cf_dup_count = cf_ok["_bank_ref"].duplicated().sum()

        lookup_cols = [c for c in ["_bank_ref", "Amount", "Agent Name", "Transaction Time"] if c in cf_ok.columns]
        cf_lookup = cf_ok[lookup_cols].rename(columns={
            "Amount":           "Report_Amount",
            "Agent Name":       "CF_Agent",
            "Transaction Time": "CF_Time",
        })

        merged = remaining.merge(cf_lookup, left_on="_utr_str", right_on="_bank_ref", how="left")
        cf_found  = merged["_bank_ref"].notna()
        
        # Safely compare amounts
        merged["_base_amt"] = pd.to_numeric(merged["_amount_raw"], errors="coerce")
        merged["_cf_amt"] = pd.to_numeric(merged["Report_Amount"], errors="coerce")
        amt_exact = merged["_base_amt"] == merged["_cf_amt"]

        # Matched
        hit = merged[cf_found & amt_exact].copy()
        hit["Recon_Status"] = "Matched"
        hit["Recon_Reason"] = "Verified in Cashfree"
        matched_list.append(hit)

        # Amount Mismatch
        amt_mismatch = merged[cf_found & ~amt_exact].copy()
        amt_mismatch["Recon_Status"] = "Amount Mismatch"
        amt_mismatch["Recon_Reason"] = "UTR found but amount differs"
        unmatched_list.append(amt_mismatch)

        # Not Found
        not_found = merged[~cf_found].copy()
        not_found["Recon_Status"] = "Not Found"
        not_found["Recon_Reason"] = "UTR absent from Cashfree"
        unmatched_list.append(not_found)

    else:
        remaining["Recon_Status"] = "Not Found"
        remaining["Recon_Reason"] = "No Cashfree report uploaded"
        unmatched_list.append(remaining)

    # Compile final datasets
    matched = pd.concat(matched_list, ignore_index=True) if matched_list else pd.DataFrame(columns=remaining.columns)
    unmatched = pd.concat(unmatched_list, ignore_index=True) if unmatched_list else pd.DataFrame(columns=remaining.columns)

    return {
        "matched":       matched,
        "unmatched":     unmatched,
        "manual_review": manual_review,
        "carrum_scope":  c_count,
        "ub_scope":      ub_count,
        "cf_scope":      len(cf_df) if cf_df is not None else 0,
        "cf_dup_warning": cf_dup_count > 0,
        "cf_dup_count":  cf_dup_count,
    }


def _empty_result(c_scope, ub_scope, cf_scope):
    empty = pd.DataFrame()
    return {
        "matched": empty, "unmatched": empty, "manual_review": empty,
        "carrum_scope": c_scope, "ub_scope": ub_scope, "cf_scope": cf_scope,
        "cf_dup_warning": False, "cf_dup_count": 0,
    }


def call_api(token: str, rows: pd.DataFrame, status: str, remarks: str) -> requests.Response:
    """Bulk review API — strictly targets Carrum records with Transaction IDs."""
    payload = [
        {
            "id": str(row["Transaction ID"]),
            "status": status,
            "review_remarks": remarks,
        }
        for _, row in rows.iterrows()
        if pd.notna(row.get("Transaction ID")) and str(row.get("Transaction ID")).strip() != ""
    ]
    
    if not payload:
        raise ValueError("No valid Carrum Transaction IDs found in the selection.")

    return requests.post(
        API_URL,
        json=payload,
        headers={
            "Authorization": f"carrum {token}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )


def render_table(df: pd.DataFrame, extra=None) -> pd.DataFrame:
    cols = [c for c in DISPLAY_COLS if c in df.columns]
    if extra:
        cols += [c for c in extra if c in df.columns and c not in cols]
    out = df[cols].reset_index(drop=True)
    st.dataframe(out, use_container_width=True, hide_index=True)
    return out


def to_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


def fmt_inr(amount) -> str:
    return f"₹{float(amount):,.0f}"

# ─── Sidebar ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("🔑 API Token")
    api_token = st.text_input(
        "JWT (without 'carrum ' prefix)",
        type="password",
        placeholder="eyJhbGci…",
    )
    st.divider()
    st.header("⚙️ Remarks")
    approve_remarks = st.text_input("Approval remarks", value="finance_auto_reconciled")
    manual_remarks = st.text_input("Manual-review remarks", value="utr_unmatched_manual_check")
    st.divider()
    st.caption("Carrum Mobility · Finance Tool")
    st.caption(datetime.now().strftime("Session: %d %b %Y, %H:%M"))

# ─── Header ──────────────────────────────────────────────────────────────────

st.title("🔄 Finance Reconciliation")
st.caption("Reconcile **Carrum (Uber Go)** or **Uber Black** primary reports against **Cashfree** settlements.")

# ─── Step 1 · Upload ─────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("Step 1 · Upload Files")

col_car, col_ub, col_cf = st.columns(3)
with col_car:
    st.markdown("**Carrum Portal (Optional)**")
    carrum_file = st.file_uploader("Drag or browse", type=["csv"], key="carrum_upload", label_visibility="collapsed")
    if carrum_file: st.success(f"✔ {carrum_file.name}")

with col_ub:
    st.markdown("**Uber Black (Optional)**")
    ub_file = st.file_uploader("Drag or browse", type=["xlsx", "xls"], key="ub_upload", label_visibility="collapsed")
    if ub_file: st.success(f"✔ {ub_file.name}")

with col_cf:
    st.markdown("**Cashfree Report**")
    cf_file = st.file_uploader("Drag or browse", type=["xlsx", "xls"], key="cf_upload", label_visibility="collapsed")
    if cf_file: st.success(f"✔ {cf_file.name}")

# ─── Step 2 · Reconcile ──────────────────────────────────────────────────────

if carrum_file or ub_file:
    file_sig = (
        carrum_file.name if carrum_file else "", carrum_file.size if carrum_file else 0,
        ub_file.name if ub_file else "", ub_file.size if ub_file else 0,
        cf_file.name if cf_file else "", cf_file.size if cf_file else 0,
    )
    if st.session_state.get("_file_sig") != file_sig:
        st.session_state.pop("_recon", None)
        st.session_state.pop("_approval_done", None)
        st.session_state["_file_sig"] = file_sig

    st.markdown("---")
    st.subheader("Step 2 · Run Reconciliation")

    if st.button("▶ Run Reconciliation", type="primary"):
        try:
            carrum_df, ub_df, cf_df = None, None, None
            
            if carrum_file:
                carrum_df = pd.read_csv(carrum_file)
                if missing_carrum := CARRUM_REQUIRED - set(carrum_df.columns):
                    st.error(f"Carrum CSV is missing: `{missing_carrum}`")
                    st.stop()
            
            if ub_file:
                raw_ub = pd.read_excel(ub_file, header=0)
                if "UTR" not in raw_ub.columns and len(raw_ub) > 1:
                    raw_ub.columns = raw_ub.iloc[0]
                    raw_ub = raw_ub.iloc[1:].reset_index(drop=True)
                if missing_ub := UB_REQUIRED - set(raw_ub.columns):
                    st.error(f"Uber Black file is missing: `{missing_ub}`")
                    st.stop()
                ub_df = raw_ub

            if cf_file:
                cf_df = pd.read_excel(cf_file)
                if missing_cf := CF_REQUIRED - set(cf_df.columns):
                    st.error(f"Cashfree file is missing: `{missing_cf}`")
                    st.stop()

            with st.spinner("Matching records…"):
                st.session_state["_recon"] = reconcile(carrum_df, ub_df, cf_df)
                st.session_state["_approval_done"] = False

        except Exception as e:
            st.error(f"Unexpected error: {e}")

    # ─── Step 3 · Review results ─────────────────────────────────────────────

    if "_recon" in st.session_state:
        res       = st.session_state["_recon"]
        matched   = res["matched"]
        unmatched = res["unmatched"]
        manual    = res["manual_review"]

        st.markdown("---")
        st.subheader("Step 3 · Review Results")

        # ── Clear UX Metrics ──
        total_primary = res['carrum_scope'] + res['ub_scope']
        
        carrum_matched = len(matched[matched["Data_Source"] == "Carrum"]) if not matched.empty else 0
        ub_matched = len(matched[matched["Data_Source"] == "Uber Black"]) if not matched.empty else 0
        
        carrum_unmatched = len(unmatched[unmatched["Data_Source"] == "Carrum"]) if not unmatched.empty else 0
        ub_unmatched = len(unmatched[unmatched["Data_Source"] == "Uber Black"]) if not unmatched.empty else 0
        
        carrum_manual = len(manual[manual["Data_Source"] == "Carrum"]) if not manual.empty else 0
        ub_manual = len(manual[manual["Data_Source"] == "Uber Black"]) if not manual.empty else 0

        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("📊 Total Processed Source Rows", total_primary)
            st.caption(f"**Carrum:** {res['carrum_scope']} rows  \n**Uber Black:** {res['ub_scope']} rows")
        with m2:
            st.metric("✅ Total Matched", len(matched))
            st.caption(f"**Carrum:** {carrum_matched} matched  \n**Uber Black:** {ub_matched} matched")
        with m3:
            st.metric("⚠️ Total Unmatched & Manual", len(unmatched) + len(manual))
            st.caption(f"**Carrum:** {carrum_unmatched + carrum_manual} flagged  \n**Uber Black:** {ub_unmatched + ub_manual} flagged")

        if res["cf_dup_warning"]:
            st.warning(f"⚠️ {res['cf_dup_count']} duplicate Bank Reference No. found in Cashfree.")

        # ── Tabs ─────────────────────────────────────────────────────────────
        tab1, tab2, tab3 = st.tabs([
            f"✅ Auto-Approvable ({len(matched)})",
            f"⚠️ Unmatched ({len(unmatched)})",
            f"❓ Manual Review ({len(manual)})"
        ])

        with tab1:
            if matched.empty: st.info("No matched transactions.")
            else:
                tbl = render_table(matched, extra=["Report_Amount", "CF_Agent", "CF_Time"])
                st.download_button("⬇️ Download Matched", data=to_csv(tbl), file_name="matched.csv", mime="text/csv")

        with tab2:
            if unmatched.empty: st.success("No unmatched rows.")
            else:
                tbl = render_table(unmatched, extra=["Recon_Status", "Recon_Reason", "Report_Amount"])
                st.download_button("⬇️ Download Unmatched", data=to_csv(tbl), file_name="unmatched.csv", mime="text/csv")
                
                carrum_unmatched_df = unmatched[unmatched["Data_Source"] == "Carrum"]
                if not carrum_unmatched_df.empty:
                    st.divider()
                    if st.button(f"📋 Mark {len(carrum_unmatched_df)} Carrum Unmatched as Manual in Portal", key="btn_man_unmatched"):
                        if not api_token: st.warning("Add JWT token in sidebar.")
                        else:
                            try:
                                r = call_api(api_token, carrum_unmatched_df, "manual_review", manual_remarks)
                                st.success("Updated Carrum API.")
                            except Exception as e: st.error(str(e))

        with tab3:
            if manual.empty: st.success("No invalid UTRs.")
            else:
                tbl = render_table(manual, extra=["Recon_Status", "Recon_Reason"])
                st.download_button("⬇️ Download Manual", data=to_csv(tbl), file_name="manual.csv", mime="text/csv")

        # ─── Step 4 · Approve ────────────────────────────────────────────────
        st.markdown("---")
        st.subheader("Step 4 · Confirm & Approve Carrum Data")

        carrum_approvals = matched[matched["Data_Source"] == "Carrum"] if not matched.empty else pd.DataFrame()

        if carrum_approvals.empty:
            st.info("No matched Carrum transactions to approve. (Uber Black transactions are local only and cannot be sent to the API).")
        elif not api_token:
            st.warning("⚠️ Enter your JWT token in the sidebar to proceed.")
        elif st.session_state.get("_approval_done"):
            st.success("✅ Approval executed.")
        else:
            amt = carrum_approvals["_base_amt"].sum()
            st.warning(f"Ready to approve **{len(carrum_approvals)} Carrum transactions** ({fmt_inr(amt)}) via API.")
            
            if st.checkbox("Confirm review of matched list"):
                if st.button(f"🚀 Approve {len(carrum_approvals)} Carrum Records", type="primary"):
                    with st.spinner("Sending approvals to Carrum API…"):
                        try:
                            resp = call_api(api_token, carrum_approvals, "approved", approve_remarks)
                            if resp.status_code in (200, 201):
                                st.success("✅ Success.")
                                st.session_state["_approval_done"] = True
                            else: st.error(f"API Error {resp.status_code}: {resp.text}")
                        except Exception as e: st.error(f"Error: {e}")

else:
    st.info("👆 Upload Carrum (Uber Go) data, Uber Black data, or both to begin.")
