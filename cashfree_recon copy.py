"""
Carrum Mobility — Finance Reconciliation Tool
Independent parallel workflows: Carrum vs Cashfree OR Uber Black vs Cashfree.
"""

from datetime import datetime
import pandas as pd
import requests
import streamlit as st

# ─── Page config ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Finance Recon",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
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
UB_REQUIRED = {"UTR", "Amount"}
CF_REQUIRED = {"Transaction Status", "Bank Reference No.", "Amount"}

CARRUM_DISPLAY_COLS = [
    "Transaction ID", "UTR Number", "Driver Name",
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

def reconcile(mode: str, primary_df: pd.DataFrame, cf_df: pd.DataFrame) -> dict:
    """
    Reconciliation engine for either Carrum or Uber Black against Cashfree.
    """
    is_carrum = mode == "Carrum (Uber Go)"

    # 1. Prepare Primary Scope
    if is_carrum:
        scope = primary_df[
            (primary_df["Payment Mode"].str.strip().str.lower() == "other") &
            (primary_df["Approval Status"].str.strip().str.lower() == "pending") &
            (primary_df["Payment Status"].str.strip().str.lower() == "success")
        ].copy()
        utr_col = "UTR Number"
    else:
        scope = primary_df.copy()
        utr_col = "UTR"

    if scope.empty:
        return _empty_result()

    # 2. Prepare Cashfree Scope
    cf_ok = cf_df[
        (cf_df["Transaction Status"].str.strip().str.upper() == "SUCCESS") &
        (cf_df["Amount"].fillna(0).astype(float) > 0)
    ].copy()

    cf_dup_count = 0
    if not cf_ok.empty:
        cf_ok["_bank_ref"] = cf_ok["Bank Reference No."].astype(str).str.strip().apply(
            lambda x: x.split(".")[0] if "." in x else x
        )
        cf_dup_count = cf_ok["_bank_ref"].duplicated().sum()
        
        lookup_cols = [c for c in ["_bank_ref", "Amount", "Agent Name", "Transaction Time"] if c in cf_ok.columns]
        cf_lookup = cf_ok[lookup_cols].rename(columns={
            "Amount": "CF_Amount",
            "Agent Name": "CF_Agent",
            "Transaction Time": "CF_Time",
        })
    else:
        cf_lookup = pd.DataFrame(columns=["_bank_ref", "CF_Amount", "CF_Agent", "CF_Time"])

    # 3. Classify UTRs in Primary Scope
    parsed = scope[utr_col].apply(parse_utr)
    scope["_utr_str"] = parsed.apply(lambda x: x[0])
    scope["_utr_valid"] = parsed.apply(lambda x: x[1])

    # Manual Review Bucket (Invalid UTRs)
    manual_review = scope[~scope["_utr_valid"]].copy()
    manual_review["Recon_Status"] = "Manual Review"
    manual_review["Recon_Reason"] = manual_review["_utr_str"].apply(
        lambda x: "Blank UTR" if x == "" else "Non-numeric / text UTR"
    )

    # 4. Match Valid UTRs
    valid = scope[scope["_utr_valid"]].copy()
    merged = valid.merge(cf_lookup, left_on="_utr_str", right_on="_bank_ref", how="left")

    cf_found = merged["_bank_ref"].notna()
    amt_exact = merged["Amount"].astype(float) == merged["CF_Amount"].astype(float)

    # Matched Bucket
    matched = merged[cf_found & amt_exact].copy()
    matched["Recon_Status"] = "Matched"

    # Unmatched Buckets
    amt_mismatch = merged[cf_found & ~amt_exact].copy()
    amt_mismatch["Recon_Status"] = "Amount Mismatch"
    amt_mismatch["Recon_Reason"] = "UTR in Cashfree but amount differs"

    not_found = merged[~cf_found].copy()
    not_found["Recon_Status"] = "Not Found"
    not_found["Recon_Reason"] = "UTR not found in Cashfree report"

    unmatched = pd.concat([not_found, amt_mismatch], ignore_index=True)

    return {
        "matched": matched,
        "unmatched": unmatched,
        "manual_review": manual_review,
        "primary_scope": len(scope),
        "cf_scope": len(cf_ok),
        "cf_dup_warning": cf_dup_count > 0,
        "cf_dup_count": cf_dup_count,
    }

def _empty_result():
    empty = pd.DataFrame()
    return {
        "matched": empty, "unmatched": empty, "manual_review": empty,
        "primary_scope": 0, "cf_scope": 0, "cf_dup_warning": False, "cf_dup_count": 0,
    }

def call_api(token: str, rows: pd.DataFrame, status: str, remarks: str) -> requests.Response:
    """Bulk review API — single POST with all transactions."""
    payload = [{"id": str(r["Transaction ID"]), "status": status, "review_remarks": remarks} for _, r in rows.iterrows()]
    return requests.post(API_URL, json=payload, headers={"Authorization": f"carrum {token}", "Content-Type": "application/json"}, timeout=60)

def render_table(df: pd.DataFrame, is_carrum: bool, extra=None) -> pd.DataFrame:
    """Render table dynamically based on primary source columns."""
    base_cols = CARRUM_DISPLAY_COLS if is_carrum else ["UTR", "Amount"]
    
    cols = [c for c in base_cols if c in df.columns]
    
    # If Uber Black, grab any other natural columns it came with (e.g. Hub, Date)
    if not is_carrum:
        for c in df.columns:
            if c not in cols and not c.startswith("_") and c not in ["Recon_Status", "Recon_Reason", "CF_Amount", "CF_Agent", "CF_Time"]:
                cols.append(c)
                
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
    api_token = st.text_input("JWT (without 'carrum ' prefix)", type="password", placeholder="eyJhbGci…")

    st.divider()
    st.header("⚙️ Remarks")
    approve_remarks = st.text_input("Approval remarks", value="finance_auto_reconciled")
    manual_remarks = st.text_input("Manual-review remarks", value="utr_unmatched_manual_check")

    st.divider()
    st.caption("Carrum Mobility · Finance Tool")
    st.caption(datetime.now().strftime("Session: %d %b %Y, %H:%M"))

# ─── Header ──────────────────────────────────────────────────────────────────

st.title("🔄 Finance Reconciliation")
st.caption("Match your primary data (Carrum or Uber Black) against the Cashfree report.")

# ─── Step 1 · Mode Selection & Upload ────────────────────────────────────────

st.markdown("---")

# Toggle Mode
mode = st.radio(
    "**Step 1 · Select Reconciliation Mode**",
    ["Carrum (Uber Go)", "Uber Black"],
    horizontal=True,
)

is_carrum = mode == "Carrum (Uber Go)"

st.spacer()

col_primary, col_cf = st.columns(2)

with col_primary:
    if is_carrum:
        st.markdown("**1A. Upload Carrum CSV** (Uber Go)")
        primary_file = st.file_uploader("Drag or browse", type=["csv"], key="carrum_upload", label_visibility="collapsed")
    else:
        st.markdown("**1A. Upload Uber Black XLSX**")
        primary_file = st.file_uploader("Drag or browse", type=["xlsx", "xls"], key="ub_upload", label_visibility="collapsed")
        
    if primary_file:
        st.success(f"✔ {primary_file.name}")

with col_cf:
    st.markdown("**1B. Upload Cashfree Report** (XLSX)")
    cf_file = st.file_uploader("Drag or browse", type=["xlsx", "xls"], key="cf_upload", label_visibility="collapsed")
    if cf_file:
        st.success(f"✔ {cf_file.name}")

# ─── Step 2 · Reconcile ──────────────────────────────────────────────────────

if primary_file and cf_file:
    
    # Cache invalidation check
    file_sig = (primary_file.name, primary_file.size, cf_file.name, cf_file.size, mode)
    if st.session_state.get("_file_sig") != file_sig:
        st.session_state.pop("_recon", None)
        st.session_state.pop("_approval_done", None)
        st.session_state["_file_sig"] = file_sig

    st.markdown("---")
    st.subheader("Step 2 · Run Reconciliation")

    if st.button("▶ Run Reconciliation", type="primary"):
        try:
            # Parse Primary File
            if is_carrum:
                primary_df = pd.read_csv(primary_file)
                missing = CARRUM_REQUIRED - set(primary_df.columns)
            else:
                primary_df = pd.read_excel(primary_file, header=0)
                # Correct header if shifted
                if "UTR" not in primary_df.columns and len(primary_df) > 1:
                    primary_df.columns = primary_df.iloc[0]
                    primary_df = primary_df.iloc[1:].reset_index(drop=True)
                missing = UB_REQUIRED - set(primary_df.columns)

            if missing:
                st.error(f"Primary file is missing required columns: `{missing}`")
                st.stop()

            # Parse Cashfree File
            cf_df = pd.read_excel(cf_file)
            missing_cf = CF_REQUIRED - set(cf_df.columns)
            if missing_cf:
                st.error(f"Cashfree file is missing required columns: `{missing_cf}`")
                st.stop()

            with st.spinner("Matching records…"):
                st.session_state["_recon"] = reconcile(mode, primary_df, cf_df)
                st.session_state["_approval_done"] = False

        except Exception as e:
            st.error(f"Unexpected error: {e}")

    # ─── Step 3 · Review results ─────────────────────────────────────────────

    if "_recon" in st.session_state:
        res = st.session_state["_recon"]
        matched = res["matched"]
        unmatched = res["unmatched"]
        manual = res["manual_review"]

        st.markdown("---")
        st.subheader("Step 3 · Review Results")

        # ── Refined UX Metrics ──
        m1, m2, m3, m4, m5 = st.columns(5)
        
        primary_label = "Carrum in-scope" if is_carrum else "Uber Black rows"
        
        m1.metric(primary_label, res["primary_scope"])
        m2.metric("Cashfree rows", res["cf_scope"])
        m3.metric("✅ Matched", len(matched))
        m4.metric("⚠️ Unmatched", len(unmatched))
        m5.metric("❓ Manual review", len(manual))

        if res["cf_dup_warning"]:
            st.warning(f"⚠️ {res['cf_dup_count']} duplicate Bank Reference numbers found in the Cashfree report.")

        st.spacer()

        # ── Tabs ──
        tab1, tab2, tab3 = st.tabs([
            f"✅  Matched  ({len(matched)})",
            f"⚠️  Unmatched  ({len(unmatched)})",
            f"❓  Manual Review  ({len(manual)})",
        ])

        with tab1:
            if matched.empty:
                st.info("No matched transactions found.")
            else:
                tbl = render_table(matched, is_carrum, extra=["CF_Amount", "CF_Agent"])
                st.download_button(
                    "⬇️ Download Matched List",
                    data=to_csv(tbl),
                    file_name=f"matched_{datetime.now().strftime('%d%b_%H%M')}.csv",
                    mime="text/csv",
                )

        with tab2:
            if unmatched.empty:
                st.success("All valid rows matched successfully.")
            else:
                tbl = render_table(unmatched, is_carrum, extra=["Recon_Status", "Recon_Reason", "CF_Amount"])
                st.download_button(
                    "⬇️ Download Unmatched List",
                    data=to_csv(tbl),
                    file_name=f"unmatched_{datetime.now().strftime('%d%b_%H%M')}.csv",
                    mime="text/csv",
                )
                
                if is_carrum and api_token:
                    st.divider()
                    if st.button(f"📋 Mark {len(unmatched)} Unmatched as Manual Review", key="btn_u_man"):
                        resp = call_api(api_token, unmatched, "manual_review", manual_remarks)
                        if resp.status_code in (200, 201): st.success("✔ Marked successfully.")
                        else: st.error(f"API Error {resp.status_code}")

        with tab3:
            if manual.empty:
                st.success("No invalid UTRs found.")
            else:
                tbl = render_table(manual, is_carrum, extra=["Recon_Status", "Recon_Reason"])
                st.download_button(
                    "⬇️ Download Manual Review List",
                    data=to_csv(tbl),
                    file_name=f"manual_{datetime.now().strftime('%d%b_%H%M')}.csv",
                    mime="text/csv",
                )
                
                if is_carrum and api_token:
                    st.divider()
                    if st.button(f"📋 Mark {len(manual)} rows as Manual Review", key="btn_m_man"):
                        resp = call_api(api_token, manual, "manual_review", manual_remarks)
                        if resp.status_code in (200, 201): st.success("✔ Marked successfully.")
                        else: st.error(f"API Error {resp.status_code}")

        # ─── Step 4 · Approve (Carrum Only) ──────────────────────────────────
        st.markdown("---")
        
        if is_carrum:
            st.subheader("Step 4 · Confirm & Approve")

            if matched.empty:
                st.info("Nothing to approve in this run.")
            elif not api_token:
                st.warning("⚠️ Enter your JWT token in the sidebar to proceed.")
            elif st.session_state.get("_approval_done"):
                st.success("✅ Approval already executed this session. Reset to process new files.")
            else:
                matched_total = matched["Amount"].sum()
                st.warning(
                    f"Approving **{len(matched)} transactions** totalling **{fmt_inr(matched_total)}** via API.\n"
                    f"Remarks: `{approve_remarks}`"
                )

                if st.checkbox("I confirm all matched transactions are correct."):
                    if st.button(f"🚀 Approve {len(matched)} Transactions", type="primary"):
                        with st.spinner("Sending approvals…"):
                            try:
                                resp = call_api(api_token, matched, "approved", approve_remarks)
                                if resp.status_code in (200, 201):
                                    st.success("✅ Success — Transactions approved.")
                                    st.session_state["_approval_done"] = True
                                else:
                                    st.error(f"API returned HTTP {resp.status_code}")
                            except Exception as e:
                                st.error(f"API Error: {e}")
        else:
            # Uber Black Alternative UI
            st.subheader("Step 4 · Finalize Records")
            st.info(
                "ℹ️ **Uber Black workflow selected.**\n\n"
                "Uber Black transactions are not stored on the Carrum portal, so API approval is disabled. "
                "Please use the **CSV Download** buttons in Step 3 to export the matched results and update your records manually."
            )

else:
    st.markdown("---")
    st.info("👆 Select your mode and upload both files to begin.")
