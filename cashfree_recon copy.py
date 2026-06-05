"""
Carrum Mobility — Cashfree Reconciliation Tool
Matches Jaram portal transactions against Cashfree report, then approves via API.
"""

import io
from datetime import datetime

import pandas as pd
import requests
import streamlit as st 

# ─── Page config ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Carrum · Cashfree Recon",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .recon-badge-green  { background:#d4edda; color:#155724; padding:3px 10px;
                          border-radius:12px; font-size:13px; font-weight:600; }
    .recon-badge-amber  { background:#fff3cd; color:#856404; padding:3px 10px;
                          border-radius:12px; font-size:13px; font-weight:600; }
    .recon-badge-red    { background:#f8d7da; color:#721c24; padding:3px 10px;
                          border-radius:12px; font-size:13px; font-weight:600; }
    div[data-testid="stMetricValue"] { font-size: 2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ─── Constants ────────────────────────────────────────────────────────────────

API_URL = "https://api-dev.carrum.co.in/api/v1/payment/review"

ARAM_REQUIRED = {
    "Payment Mode", "Approval Status", "Payment Status",
    "UTR Number", "Amount", "Transaction ID",
}
CF_REQUIRED = {"Transaction Status", "Bank Reference No.", "Amount"}

DISPLAY_COLS = [
    "Transaction ID", "UTR Number", "Driver Name",
    "Driver Small ID", "Amount", "Hub",
    "Transaction Date Date", "DM Name",
]


# ─── Core logic ───────────────────────────────────────────────────────────────

def parse_utr(val) -> tuple:
    """
    Returns (clean_str: str, is_numeric: bool).
    Handles NaN, float-strings like '615590309597.0', and text notes.
    """
    if pd.isna(val) or str(val).strip() == "":
        return ("", False)
    s = str(val).strip()
    # float→str produces '615590309597.0' — strip the decimal
    if "." in s:
        s = s.split(".")[0]
    try:
        int(s)
        return (s, True)
    except ValueError:
        return (s, False)


def reconcile(jaram_df: pd.DataFrame, cf_df: pd.DataFrame) -> dict:
    """
    Reconciliation pipeline.

    Steps:
      1. Filter Jaram  → payment_mode=other, approval_status=pending, payment_status=success
      2. Classify UTRs → numeric (matchable) vs invalid/blank (manual review)
      3. Filter CF     → Transaction Status=SUCCESS, Amount > 0
      4. Merge on UTR  → left join Jaram numeric rows onto CF Bank Reference No.
      5. Categorise    → matched / unmatched (not in CF or amount mismatch) / manual_review

    NOTE: Cashfree matching key is 'Bank Reference No.' (12-digit UPI ref),
          NOT 'UTR No.' (batch settlement prefix like CB0133840860).
    """

    # ── 1. Jaram filter ──────────────────────────────────────────────────────
    scope = jaram_df[
        (jaram_df["Payment Mode"].str.strip().str.lower() == "other") &
        (jaram_df["Approval Status"].str.strip().str.lower() == "pending") &
        (jaram_df["Payment Status"].str.strip().str.lower() == "success")
    ].copy()

    if scope.empty:
        return _empty_result(len(jaram_df), 0)

    # ── 2. Classify UTRs ─────────────────────────────────────────────────────
    parsed = scope["UTR Number"].apply(parse_utr)
    scope["_utr_str"]   = parsed.apply(lambda x: x[0])
    scope["_utr_valid"] = parsed.apply(lambda x: x[1])

    manual_review = scope[~scope["_utr_valid"]].copy()
    manual_review["Recon_Status"] = "Manual Review"
    manual_review["Recon_Reason"] = manual_review["_utr_str"].apply(
        lambda x: "Blank UTR" if x == "" else "Non-numeric / text UTR"
    )

    numeric = scope[scope["_utr_valid"]].copy()

    # ── 3. CF filter ─────────────────────────────────────────────────────────
    cf_ok = cf_df[
        (cf_df["Transaction Status"].str.strip().str.upper() == "SUCCESS") &
        (cf_df["Amount"].fillna(0).astype(float) > 0)
    ].copy()

    cf_ok["_bank_ref"] = cf_ok["Bank Reference No."].astype(str).str.strip()
    cf_ok["_bank_ref"] = cf_ok["_bank_ref"].apply(
        lambda x: x.split(".")[0] if "." in x else x
    )

    # Warn if duplicates exist in CF
    cf_dup_count = cf_ok["_bank_ref"].duplicated().sum()

    # ── 4. Merge ─────────────────────────────────────────────────────────────
    lookup_cols = ["_bank_ref", "Amount", "Agent Name", "Transaction Time"]
    lookup_cols = [c for c in lookup_cols if c in cf_ok.columns]

    cf_lookup = cf_ok[lookup_cols].rename(columns={
        "Amount":           "CF_Amount",
        "Agent Name":       "CF_Agent",
        "Transaction Time": "CF_Time",
    })

    merged = numeric.merge(
        cf_lookup,
        left_on="_utr_str",
        right_on="_bank_ref",
        how="left",
    )

    # ── 5. Categorise ────────────────────────────────────────────────────────
    cf_found  = merged["_bank_ref"].notna()
    amt_exact = merged["Amount"].astype(float) == merged["CF_Amount"].astype(float)

    matched = merged[cf_found & amt_exact].copy()
    matched["Recon_Status"] = "Matched"
    matched["Recon_Reason"] = "UTR + Amount verified in Cashfree"

    amt_mismatch = merged[cf_found & ~amt_exact].copy()
    amt_mismatch["Recon_Status"] = "Amount Mismatch"
    amt_mismatch["Recon_Reason"] = "UTR in Cashfree but amount differs"

    no_cf = merged[~cf_found].copy()
    no_cf["Recon_Status"] = "Not in Cashfree"
    no_cf["Recon_Reason"] = "UTR absent from Cashfree report"

    unmatched = pd.concat([no_cf, amt_mismatch], ignore_index=True)

    return {
        "matched":       matched,
        "unmatched":     unmatched,
        "manual_review": manual_review,
        "jaram_scope":   len(scope),
        "cf_scope":      len(cf_ok),
        "cf_dup_warning": cf_dup_count > 0,
        "cf_dup_count":  cf_dup_count,
        "jaram_total":   len(jaram_df),
    }


def _empty_result(jaram_total, cf_scope):
    empty = pd.DataFrame()
    return {
        "matched": empty, "unmatched": empty, "manual_review": empty,
        "jaram_scope": 0, "cf_scope": cf_scope,
        "cf_dup_warning": False, "cf_dup_count": 0,
        "jaram_total": jaram_total,
    }


def call_api(token: str, rows: pd.DataFrame, status: str, remarks: str) -> requests.Response:
    """Bulk review API — single POST with all transactions."""
    payload = [
        {
            "id": str(row["Transaction ID"]),
            "status": status,
            "review_remarks": remarks,
        }
        for _, row in rows.iterrows()
    ]
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
    """Render a standardised table and return the displayed DataFrame."""
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
        help="From Postman / email. The app prepends 'carrum ' automatically.",
    )

    st.divider()
    st.header("⚙️ Remarks")
    approve_remarks = st.text_input(
        "Approval remarks",
        value="cashfree_auto_reconciled",
        help="Sent as review_remarks for approved transactions.",
    )
    manual_remarks = st.text_input(
        "Manual-review remarks",
        value="utr_unmatched_manual_check",
        help="Sent as review_remarks when marking rows for manual review.",
    )

    st.divider()
    st.caption("Carrum Mobility · Finance Tool")
    st.caption(datetime.now().strftime("Session: %d %b %Y, %H:%M"))


# ─── Header ──────────────────────────────────────────────────────────────────

st.title("🔄 Cashfree Reconciliation")
st.caption(
    "Matches Jaram *other + pending + success* transactions against the Cashfree report "
    "on **Bank Reference No. + Amount**. Verified matches are approved via the Carrum API."
)

# ─── Step 1 · Upload ─────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("Step 1 · Upload Files")

col_j, col_c = st.columns(2)
with col_j:
    st.markdown("**Jaram Portal CSV**")
    jaram_file = st.file_uploader("Drag or browse", type=["csv"], key="jaram_upload", label_visibility="collapsed")
    if jaram_file:
        st.success(f"✔ {jaram_file.name}  ({jaram_file.size / 1024:.1f} KB)")

with col_c:
    st.markdown("**Cashfree Report (XLSX)**")
    cf_file = st.file_uploader("Drag or browse", type=["xlsx", "xls"], key="cf_upload", label_visibility="collapsed")
    if cf_file:
        st.success(f"✔ {cf_file.name}  ({cf_file.size / 1024:.1f} KB)")

with st.expander("ℹ️ How this works", expanded=False):
    st.markdown(
        """
        | Step | What happens |
        |------|-------------|
        | Filter Jaram | `payment_mode = other`, `approval_status = pending`, `payment_status = success` |
        | Filter CF | `Transaction Status = SUCCESS`, `Amount > 0` |
        | Match key | Jaram **UTR Number** == Cashfree **Bank Reference No.** (12-digit UPI ref) |
        | Amount check | Exact rupee match — no tolerance |
        | Result buckets | ✅ Matched → approve · ⚠️ Unmatched → team review · ❓ Invalid UTR → manual |

        > **Note:** `UTR No.` in Cashfree (e.g. `CB0133840860`) is a *batch settlement ID*, not the transaction UTR.
        > The real per-transaction reference is `Bank Reference No.`
        """
    )

# ─── Step 2 · Reconcile ──────────────────────────────────────────────────────

if jaram_file and cf_file:

    # Detect file changes and invalidate cached result
    file_sig = (jaram_file.name, cf_file.name, jaram_file.size, cf_file.size)
    if st.session_state.get("_file_sig") != file_sig:
        st.session_state.pop("_recon", None)
        st.session_state.pop("_approval_done", None)
        st.session_state["_file_sig"] = file_sig

    st.markdown("---")
    st.subheader("Step 2 · Run Reconciliation")

    if st.button("▶ Run Reconciliation", type="primary"):
        try:
            jaram_df = pd.read_csv(jaram_file)
            cf_df    = pd.read_excel(cf_file)

            missing_j = JARAM_REQUIRED - set(jaram_df.columns)
            missing_c = CF_REQUIRED    - set(cf_df.columns)

            if missing_j or missing_c:
                if missing_j:
                    st.error(f"Jaram CSV is missing columns: `{missing_j}`")
                if missing_c:
                    st.error(f"Cashfree file is missing columns: `{missing_c}`")
                st.stop()

            with st.spinner("Matching records…"):
                st.session_state["_recon"]        = reconcile(jaram_df, cf_df)
                st.session_state["_approval_done"] = False

        except pd.errors.ParserError as e:
            st.error(f"File parse error: {e}")
        except Exception as e:
            st.error(f"Unexpected error: {e}")
            import traceback
            st.code(traceback.format_exc())

    # ─── Step 3 · Review results ─────────────────────────────────────────────

    if "_recon" in st.session_state:
        res     = st.session_state["_recon"]
        matched = res["matched"]
        unmatched = res["unmatched"]
        manual    = res["manual_review"]

        st.markdown("---")
        st.subheader("Step 3 · Review Results")

        # ── Metrics ──────────────────────────────────────────────────────────
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Jaram in-scope",     res["jaram_scope"],  help="other + pending + success")
        m2.metric("Cashfree in-scope",  res["cf_scope"],     help="SUCCESS + Amount > 0")
        m3.metric("✅ Auto-approvable", len(matched))
        m4.metric("⚠️ Unmatched",       len(unmatched))
        m5.metric("❓ Manual review",   len(manual))

        if res["cf_dup_warning"]:
            st.warning(
                f"⚠️ {res['cf_dup_count']} duplicate Bank Reference No. found in Cashfree. "
                "Review duplicates before approving."
            )

        if len(matched):
            matched_total = matched["Amount"].sum()
            st.success(
                f"**{len(matched)} transactions · {fmt_inr(matched_total)}** verified and ready to approve."
            )
        else:
            st.info("No transactions matched both UTR and Amount in Cashfree.")

        # ── Tabs ─────────────────────────────────────────────────────────────
        tab1, tab2, tab3 = st.tabs([
            f"✅  Auto-Approvable  ({len(matched)})",
            f"⚠️  Unmatched  ({len(unmatched)})",
            f"❓  Manual Review  ({len(manual)})",
        ])

        with tab1:
            if matched.empty:
                st.info("No matched transactions in this run.")
            else:
                st.caption(
                    "UTR Number == Bank Reference No. AND Jaram Amount == Cashfree Amount. "
                    "These will be sent as **approved** once you confirm below."
                )
                tbl = render_table(matched, extra=["CF_Amount", "CF_Agent", "CF_Time"])
                ts  = datetime.now().strftime("%d%b%Y_%H%M")
                st.download_button(
                    "⬇️ Download Matched List",
                    data=to_csv(tbl),
                    file_name=f"matched_{ts}.csv",
                    mime="text/csv",
                    key="dl_matched",
                )

        with tab2:
            if unmatched.empty:
                st.success("All numeric-UTR rows were found in Cashfree.")
            else:
                st.caption(
                    "Numeric UTRs not found in Cashfree, or UTR found but amount differs. "
                    "Likely EDC / card machine transactions — requires separate reconciliation."
                )
                tbl = render_table(
                    unmatched,
                    extra=["Recon_Status", "Recon_Reason", "CF_Amount"],
                )
                ts = datetime.now().strftime("%d%b%Y_%H%M")
                st.download_button(
                    "⬇️ Download Unmatched List",
                    data=to_csv(tbl),
                    file_name=f"unmatched_{ts}.csv",
                    mime="text/csv",
                    key="dl_unmatched",
                )

                st.divider()
                st.caption(
                    "Optionally mark these as **manual_review** in the portal "
                    "(does not approve them — flags for the team)."
                )
                if not api_token:
                    st.warning("Add your JWT token in the sidebar to use this.")
                else:
                    if st.button(
                        f"📋 Mark {len(unmatched)} Unmatched as Manual Review",
                        key="btn_manual_unmatched",
                    ):
                        with st.spinner("Calling API…"):
                            try:
                                r = call_api(api_token, unmatched, "manual_review", manual_remarks)
                                if r.status_code in (200, 201):
                                    st.success(f"✔ {len(unmatched)} rows marked as manual_review.")
                                    try: st.json(r.json())
                                    except: st.code(r.text)
                                elif r.status_code == 401:
                                    st.error("401 — Token expired or invalid.")
                                else:
                                    st.error(f"API returned {r.status_code}")
                                    st.code(r.text)
                            except Exception as e:
                                st.error(str(e))

        with tab3:
            if manual.empty:
                st.success("No invalid-UTR transactions.")
            else:
                st.caption(
                    "Blank UTRs or text entries (e.g. 'Referred to …'). "
                    "Cannot be auto-matched — require human verification."
                )
                tbl = render_table(manual, extra=["Recon_Status", "Recon_Reason"])
                ts  = datetime.now().strftime("%d%b%Y_%H%M")
                st.download_button(
                    "⬇️ Download Manual Review List",
                    data=to_csv(tbl),
                    file_name=f"manual_{ts}.csv",
                    mime="text/csv",
                    key="dl_manual",
                )

                st.divider()
                st.caption("Optionally mark these as **manual_review** in the portal.")
                if not api_token:
                    st.warning("Add your JWT token in the sidebar.")
                else:
                    if st.button(
                        f"📋 Mark {len(manual)} Invalid-UTR rows as Manual Review",
                        key="btn_manual_invalid",
                    ):
                        with st.spinner("Calling API…"):
                            try:
                                r = call_api(api_token, manual, "manual_review", manual_remarks)
                                if r.status_code in (200, 201):
                                    st.success(f"✔ {len(manual)} rows marked as manual_review.")
                                    try: st.json(r.json())
                                    except: st.code(r.text)
                                elif r.status_code == 401:
                                    st.error("401 — Token expired or invalid.")
                                else:
                                    st.error(f"API returned {r.status_code}")
                                    st.code(r.text)
                            except Exception as e:
                                st.error(str(e))

        # ── Full report download ──────────────────────────────────────────────
        st.divider()
        if not matched.empty or not unmatched.empty or not manual.empty:
            all_parts = []
            for df in [matched, unmatched, manual]:
                if not df.empty:
                    all_parts.append(df)
            if all_parts:
                full_report = pd.concat(all_parts, ignore_index=True)
                keep = [c for c in DISPLAY_COLS + ["Recon_Status", "Recon_Reason", "CF_Amount", "CF_Agent"]
                        if c in full_report.columns]
                ts = datetime.now().strftime("%d%b%Y_%H%M")
                st.download_button(
                    "📥 Download Full Reconciliation Report",
                    data=to_csv(full_report[keep]),
                    file_name=f"recon_full_{ts}.csv",
                    mime="text/csv",
                    key="dl_full",
                )

        # ─── Step 4 · Approve ────────────────────────────────────────────────

        st.markdown("---")
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
                f"You are about to approve **{len(matched)} transactions** "
                f"totalling **{fmt_inr(matched_total)}** via the Carrum API.  \n"
                f"Remarks that will be sent: `{approve_remarks}`  \n"
                f"**This action is irreversible.**"
            )

            confirmed = st.checkbox(
                "I have reviewed the matched list in Step 3 and confirm all transactions above are correct."
            )

            if confirmed:
                if st.button(
                    f"🚀 Approve {len(matched)} Transactions ({fmt_inr(matched_total)})",
                    type="primary",
                    key="btn_approve",
                ):
                    with st.spinner(f"Sending {len(matched)} approvals to Carrum API…"):
                        try:
                            resp = call_api(api_token, matched, "approved", approve_remarks)

                            if resp.status_code in (200, 201):
                                st.success(
                                    f"✅ Success — API returned {resp.status_code}.  \n"
                                    f"{len(matched)} transactions approved."
                                )
                                st.session_state["_approval_done"] = True
                                try:
                                    st.json(resp.json())
                                except Exception:
                                    st.code(resp.text)

                            elif resp.status_code == 401:
                                st.error(
                                    "401 Unauthorized — your JWT token has likely expired. "
                                    "Paste a fresh token in the sidebar and try again."
                                )
                            elif resp.status_code == 422:
                                st.error(
                                    f"422 Unprocessable Entity — the API rejected the payload. "
                                    f"Check transaction IDs and status values."
                                )
                                st.code(resp.text)
                            else:
                                st.error(f"API returned HTTP {resp.status_code}")
                                st.code(resp.text)

                        except requests.Timeout:
                            st.error(
                                "Request timed out (60 s). The Carrum API may be slow. "
                                "Wait and try again — do NOT re-submit without confirming "
                                "in the portal whether the approvals went through."
                            )
                        except requests.ConnectionError:
                            st.error(
                                "Cannot reach api.carrum.co.in. "
                                "Check your network / VPN and try again."
                            )
                        except Exception as e:
                            st.error(f"Unexpected error: {e}")

        # ─── Reset ───────────────────────────────────────────────────────────
        st.markdown("---")
        if st.button("🔄 Reset — Upload New Files", key="btn_reset"):
            for k in ["_recon", "_file_sig", "_approval_done"]:
                st.session_state.pop(k, None)
            st.rerun()

else:
    st.markdown("---")
    st.info("👆 Upload both files above to begin. The reconciliation runs in seconds.")
