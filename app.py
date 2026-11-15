import io, json
import pandas as pd
import matplotlib.pyplot as plt
import streamlit as st
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

import sandbox_core as core

st.set_page_config(page_title="SimTrading", layout="wide")
FNAME = "investing_sandbox_state.json"


# ---------- Google Drive (service account, file lives in a folder you share with it) ----------
@st.cache_resource
def drive():
    info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find(svc):
    folder = st.secrets["drive_folder_id"]
    q = f"name='{FNAME}' and '{folder}' in parents and trashed=false"
    files = svc.files().list(q=q, fields="files(id)", spaces="drive").execute().get("files", [])
    return files[0]["id"] if files else None


def load_state():
    svc = drive()
    fid = _find(svc)
    if not fid:
        return None
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=fid))
    done = False
    while not done:
        _, done = dl.next_chunk()
    return json.loads(buf.getvalue().decode())


def save_state(state):
    svc = drive()
    media = MediaIoBaseUpload(io.BytesIO(json.dumps(state, indent=1).encode()), mimetype="application/json")
    fid = _find(svc)
    if fid:
        svc.files().update(fileId=fid, media_body=media).execute()
    else:
        svc.files().create(body={"name": FNAME, "parents": [st.secrets["drive_folder_id"]]},
                           media_body=media, fields="id").execute()


_orig_fetch = core.fetch


@st.cache_data(ttl=900, show_spinner=False)
def cached_fetch(tickers, start):
    return _orig_fetch(tuple(tickers), start)


core.fetch = cached_fetch  # core.refresh and core.place_trade use the cached price fetch


# ---------- state ----------
if "state" not in st.session_state:
    try:
        st.session_state.state = load_state() or core.new_state()
    except Exception as e:
        st.error(f"Could not read Drive: {e}")
        st.stop()
S = st.session_state.state


def persist():
    try:
        save_state(S)
    except Exception as e:
        st.error(f"Saved in this session only. Drive save failed: {e}")


def fail(e):
    st.error(str(e))


n_before, div_before = len(S["events"]), S["last_div_date"]
try:
    with st.spinner("Fetching prices..."):
        hist, r, bench = core.refresh(S)   # opening and dividend processing can add events
except Exception as e:
    st.error(f"Stopped: {e}")
    st.stop()
if len(S["events"]) != n_before or S["last_div_date"] != div_before:
    persist()

st.title("SimTrading")
st.caption(f"Latest price date: {r.index[-1].date()} · Benchmark: SPY total return · Prices from yfinance")
tab_dash, tab_shares, tab_trades, tab_settings, tab_thesis, tab_rules, tab_hist = st.tabs(
    ["Dashboard", "Shares", "Trades", "Settings", "Thesis", "Rules", "Settings history"])

# ---------- Dashboard ----------
with tab_dash:
    m = core.metrics(S, r, bench)
    cols = st.columns(5)
    for i, (k, v) in enumerate(m):
        cols[i % 5].metric(k, v)
    fig = None
    base = r["net"].iloc[0]
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    ax[0].plot(r.index, r["net"], color="#4fc3f7", label="Sandbox (net)")
    ax[0].plot(bench.index, bench, color="#ffb74d", label="SPY total return")
    ax[0].set_title("Value ($)")
    ax[1].plot(r.index, (r["net"] / base - 1) * 100, color="#4fc3f7", label="Sandbox")
    ax[1].plot(bench.index, (bench / bench.iloc[0] - 1) * 100, color="#ffb74d", label="SPY")
    ax[1].set_title("Return since start (%)")
    for a in ax:
        a.grid(True, alpha=0.25)
        a.legend(fontsize=9, frameon=False)
        a.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    st.pyplot(fig)
    st.subheader("Settings segments")
    st.caption("Each segment uses the settings in force during it. Compare segments of similar length.")
    st.dataframe(pd.DataFrame(core.segment_rows(S, r, bench),
                              columns=["From", "To", "Sandbox", "SPY total return", "Difference"]),
                 hide_index=True, use_container_width=True)

# ---------- Shares ----------
with tab_shares:
    st.subheader("Holdings")
    st.dataframe(pd.DataFrame(core.holdings_rows(S, hist),
                              columns=["Ticker", "Shares", "Last price", "Market value", "Weight", "DRIP %", "Target"]),
                 hide_index=True, use_container_width=True)
    st.subheader("Dividend reinvestment and targets")
    with st.form("holdings_form"):
        sliders, targets = {}, {}
        for t in sorted(set(S["tickers"])):
            c1, c2 = st.columns([3, 1])
            sliders[t] = c1.slider(f"{t} DRIP %", 0.0, 100.0, float(core.get_setting(S, f"reinvest_{t}", core.TODAY, 0.0)),
                                   1.0, key=f"dr_{t}")
            targets[t] = c2.number_input(f"{t} target %", 0.0, 100.0, float(S["targets"].get(t, 0.0) * 100), 0.5,
                                         key=f"tg_{t}")
        hreason = st.text_input("Reason for these changes (logged)")
        if st.form_submit_button("Apply DRIP % and targets", type="primary"):
            if not hreason.strip():
                st.error("Give a reason for the change.")
            else:
                for t in sliders:
                    if abs(core.get_setting(S, f"reinvest_{t}", core.TODAY, 0.0) - sliders[t]) > 1e-9:
                        core.log_setting(S, f"reinvest_{t}", float(sliders[t]), core.TODAY, hreason.strip())
                    w = targets[t] / 100
                    if w > 0:
                        S["targets"][t] = w
                    else:
                        S["targets"].pop(t, None)
                persist()
                st.rerun()
    with st.form("add_ticker"):
        new_t = st.text_input("Add a ticker, e.g. VOO")
        if st.form_submit_button("Add ticker") and new_t.strip():
            try:
                core.ensure_ticker(S, new_t.strip().upper())
                persist()
                st.rerun()
            except Exception as e:
                fail(e)
    if st.button("Rebalance to targets now"):
        try:
            rb_hist = core.fetch(sorted(set(S["tickers"]) | {core.BENCH}), S["start_date"])
            core.rebalance(S, rb_hist, r, "Rebalance to targets")
            persist()
            st.rerun()
        except Exception as e:
            fail(e)

# ---------- Trades ----------
with tab_trades:
    with st.form("trade_form"):
        c1, c2, c3 = st.columns([1, 1, 1])
        t_ticker = c1.text_input("Ticker").strip().upper()
        t_side = c2.selectbox("Side", ["BUY", "SELL"])
        t_amt = c3.number_input("$ amount", min_value=0.0, value=0.0, step=100.0)
        t_reason = st.text_input("Why (required)")
        if st.form_submit_button("Log trade", type="primary"):
            if not t_ticker or not t_reason.strip() or t_amt <= 0:
                st.error("Ticker, amount, and reason are required.")
            else:
                try:
                    core.ensure_ticker(S, t_ticker)
                    th = core.fetch(sorted(set(S["tickers"]) | {core.BENCH}), S["start_date"])
                    core.check_splits(th, S["start_date"])
                    core.place_trade(S, th, t_ticker, t_side, t_amt, t_reason.strip())
                    persist()
                    st.rerun()
                except Exception as e:
                    fail(e)
    st.subheader("Trade and dividend log")
    rows = [[e["date"], e["kind"], e["ticker"], round(e["shares"], 4), round(e["price"], 2),
             round(e["cash"], 2), round(e["fee"], 2), e["reason"]]
            for e in sorted(S["events"], key=lambda e: e["date"], reverse=True) if e["kind"] != "CASH"]
    st.dataframe(pd.DataFrame(rows, columns=["Date", "Type", "Ticker", "Shares", "Price", "Cash effect", "Fee", "Reason"]),
                 hide_index=True, use_container_width=True)

# ---------- Settings ----------
with tab_settings:
    with st.form("settings_form"):
        spread = st.slider("Bid-ask spread %", 0.0, 1.0, float(core.get_setting(S, "spread_pct", core.TODAY, 0.05)), 0.01)
        fee = st.number_input("Fee $ per trade", min_value=0.0, value=float(core.get_setting(S, "fee", core.TODAY, 0.0)))
        tax = st.slider("Estimated tax %", 0.0, 50.0, float(core.get_setting(S, "tax_rate", core.TODAY, 15.0)), 0.5)
        frac = st.checkbox("Fractional shares", value=bool(core.get_setting(S, "fractional", core.TODAY, True)))
        reb = st.selectbox("Rebalance note (no automatic trades run)", ["Manual", "Monthly", "Quarterly"],
                           index=["Manual", "Monthly", "Quarterly"].index(core.get_setting(S, "rebalance", core.TODAY, "Manual")))
        sreason = st.text_input("Reason for setting changes (logged)")
        if st.form_submit_button("Apply settings", type="primary"):
            if not sreason.strip():
                st.error("Give a reason for setting changes.")
            else:
                new = {"spread_pct": spread, "fee": fee, "tax_rate": tax, "fractional": frac, "rebalance": reb}
                changed = False
                for k, v in new.items():
                    if core.get_setting(S, k, core.TODAY) != v:
                        core.log_setting(S, k, v, core.TODAY, sreason.strip())
                        changed = True
                if changed:
                    persist()
                    st.rerun()
                else:
                    st.info("No settings changed.")

# ---------- Thesis ----------
with tab_thesis:
    with st.form("thesis_form"):
        claim = st.text_input("Claim")
        target = st.text_input("Measurable target")
        deadline = st.text_input("Deadline YYYY-MM-DD")
        if st.form_submit_button("Add thesis") and claim.strip() and deadline.strip():
            S["theses"].append({"added": core.TODAY, "claim": claim.strip(), "target": target.strip(),
                                "deadline": deadline.strip(), "outcome": "pending"})
            persist()
            st.rerun()
    if S["theses"]:
        with st.form("outcome_form"):
            idx = st.selectbox("Thesis", range(len(S["theses"])),
                               format_func=lambda i: f"{i + 1}. {S['theses'][i]['claim']}")
            outcome = st.text_input("Outcome and evidence")
            if st.form_submit_button("Record outcome") and outcome.strip():
                S["theses"][idx]["outcome"] = outcome.strip()
                persist()
                st.rerun()
    st.dataframe(pd.DataFrame(S["theses"]) if S["theses"] else pd.DataFrame(
        columns=["added", "claim", "target", "deadline", "outcome"]), hide_index=True, use_container_width=True)

# ---------- Rules ----------
with tab_rules:
    rules = st.text_area("Rules you are testing against", value=S["rules_text"], height=200)
    if st.button("Save rules"):
        S["rules_text"] = rules
        persist()
        st.success("Saved.")
    with st.form("dev_form"):
        dev = st.text_input("Deviation from the rules: what and why")
        if st.form_submit_button("Log deviation") and dev.strip():
            S["deviations"].append({"date": core.TODAY, "reason": dev.strip()})
            persist()
            st.rerun()
    st.dataframe(pd.DataFrame(S["deviations"]) if S["deviations"] else pd.DataFrame(columns=["date", "reason"]),
                 hide_index=True, use_container_width=True)

# ---------- Settings history ----------
with tab_hist:
    st.dataframe(pd.DataFrame([{k: x[k] for k in ["effective", "key", "value", "logged_at", "reason"]}
                               for x in S["settings"]]), hide_index=True, use_container_width=True)
