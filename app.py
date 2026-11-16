import json
import base64
import datetime as dt
import math
import pandas as pd
import requests
import matplotlib.pyplot as plt
import importlib
import streamlit as st

import sandbox_core as core

if not hasattr(core, "new_market"):   # a pre-update module stayed in memory across the redeploy
    importlib.reload(core)
    if hasattr(core, "_raw_fetch"):
        del core._raw_fetch

st.set_page_config(page_title="SimTrading", layout="wide")


# The core module outlives script reruns, so capture the real fetch only once. Without this each
# rerun wrapped the previous run's cached wrapper, stacking caches and deadlocking on a cache miss.
if not hasattr(core, "_raw_fetch"):
    core._raw_fetch = core.fetch
_orig_fetch = core._raw_fetch


@st.cache_data(ttl=900, show_spinner=False)
def cached_fetch_v2(tickers, start):   # new name so cached results from the old version are never reused
    return _orig_fetch(tuple(tickers), start)


core.fetch = cached_fetch_v2  # core.refresh and the order code use the cached price fetch


def fail(e):
    st.error(str(e))


# ---------- state and market data: auto-synced to this repo through a token kept in Streamlit Secrets ----------
GH_STATE = "data/investing_sandbox_state.json"
GH_MARKET = "data/market_data.json"       # every daily bar the app has used, written compactly


DEFAULT_REPO = "malcolm13234/SimTrading"   # used when the github_repo secret is not set


def secret_names():
    try:
        return sorted(st.secrets.keys())
    except Exception:       # no secrets file at all: run without syncing instead of crashing
        return []


def sync_on():
    return "github_token" in secret_names()


def gh_repo():
    return st.secrets["github_repo"] if "github_repo" in secret_names() else DEFAULT_REPO


def _gh_url(path):
    return f"https://api.github.com/repos/{gh_repo()}/contents/{path}"


def _gh_headers():
    return {"Authorization": f"Bearer {st.secrets['github_token']}", "Accept": "application/vnd.github+json"}


def gh_get(path):
    """Returns (object, sha), or (None, None) if the file does not exist yet."""
    r = requests.get(_gh_url(path), headers=_gh_headers(), timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    body = r.json()
    if body.get("content"):
        raw = base64.b64decode(body["content"])
    else:   # the contents API leaves out the body of files over 1 MB; the blob API has it
        b = requests.get(f"https://api.github.com/repos/{gh_repo()}/git/blobs/{body['sha']}",
                         headers=_gh_headers(), timeout=60)
        b.raise_for_status()
        raw = base64.b64decode(b.json()["content"])
    return json.loads(raw.decode()), body["sha"]


def gh_put(path, obj, sha, message, compact=False):
    text = json.dumps(obj, separators=(",", ":")) if compact else json.dumps(obj, indent=1)
    payload = {"message": message, "content": base64.b64encode(text.encode()).decode()}
    if sha:
        payload["sha"] = sha
    r = requests.put(_gh_url(path), headers=_gh_headers(), json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["content"]["sha"]


def persist():
    if not sync_on():
        return
    try:
        st.session_state["sha_state"] = gh_put(GH_STATE, st.session_state.state, st.session_state.get("sha_state"),
                                               f"Sandbox update {core.today()}")
    except Exception as e:
        st.sidebar.error(f"Auto-sync failed, changes are only in this session: {e}")


def persist_market():
    if not sync_on():
        return
    try:
        st.session_state["sha_market"] = gh_put(GH_MARKET, st.session_state.market, st.session_state.get("sha_market"),
                                                f"Market data through {core.today()}", compact=True)
    except Exception as e:
        st.sidebar.error(f"Market data sync failed: {e}")


if "state" not in st.session_state:
    loaded, market = None, None
    if sync_on():
        try:
            loaded, st.session_state["sha_state"] = gh_get(GH_STATE)
            market, st.session_state["sha_market"] = gh_get(GH_MARKET)
        except Exception as e:
            st.error(f"Could not load saved state from GitHub, so the app stopped to protect it: {e}")
            st.stop()
    st.session_state.state = loaded or core.new_state()
    st.session_state.market = market or core.new_market()
if not sync_on():
    st.sidebar.caption("Not syncing: no secret named github_token. Secret names this app can see: "
                       f"{secret_names() or 'none'}. Add github_token in the app's Settings, Secrets.")
else:
    st.sidebar.caption(f"Syncing to {gh_repo()}")
S = st.session_state.state
M = st.session_state.market


sig_before = core.signature(S)
try:
    with st.spinner("Fetching prices..."):
        hist, r, bench = core.refresh(S)   # opening, dividends and limit-order fills can add events
except Exception as e:
    st.error(f"Stopped: {e}")
    st.stop()
new_rows = core.merge_market(M, hist)
if core.signature(S) != sig_before:
    persist()
if new_rows:
    persist_market()

st.sidebar.download_button("Download state (.json)", data=json.dumps(S, indent=1),
                           file_name="sandbox_state.json", mime="application/json")
st.sidebar.download_button("Download market data (.json)", data=json.dumps(M, separators=(",", ":")),
                           file_name="market_data.json", mime="application/json")
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
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    ax[0].plot(r.index, r["net"], color="#4fc3f7", label="Sandbox (net)")
    ax[0].plot(bench.index, bench["value"], color="#ffb74d", label="SPY, same cash flows")
    ax[0].set_title("Value ($)")
    ax[1].plot(r.index, (r["twr"] - 1) * 100, color="#4fc3f7", label="Sandbox")
    ax[1].plot(bench.index, (bench["twr"] - 1) * 100, color="#ffb74d", label="SPY")
    ax[1].set_title("Time-weighted return since start (%)")
    for a in ax:
        a.grid(True, alpha=0.25)
        a.legend(fontsize=9, frameon=False)
        a.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    st.pyplot(fig)
    plt.close(fig)
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
            sliders[t] = c1.slider(f"{t} DRIP %", 0.0, 100.0, float(core.get_setting(S, f"reinvest_{t}", core.today(), 0.0)),
                                   1.0, key=f"dr_{t}")
            targets[t] = c2.number_input(f"{t} target %", 0.0, 100.0, float(S["targets"].get(t, 0.0) * 100), 0.5,
                                         key=f"tg_{t}")
        hreason = st.text_input("Reason for these changes (logged)")
        if st.form_submit_button("Apply DRIP % and targets", type="primary"):
            if not hreason.strip():
                st.error("Give a reason for the change.")
            else:
                for t in sliders:
                    if abs(core.get_setting(S, f"reinvest_{t}", core.today(), 0.0) - sliders[t]) > 1e-9:
                        core.log_setting(S, f"reinvest_{t}", float(sliders[t]), core.today(), hreason.strip())
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
    st.subheader("Cash")
    st.metric("Cash on hand", f"${core.cash_now(S):,.2f}")
    cd, cw = st.columns(2)
    with cd, st.form("deposit_form"):
        dep_amt = st.number_input("Deposit $ (no limit)", min_value=0.0, value=0.0, step=1000.0, format="%.2f")
        dep_reason = st.text_input("Note", value="Deposit", key="dep_note")
        if st.form_submit_button("Deposit", type="primary"):
            try:
                core.deposit(S, hist, dep_amt, dep_reason.strip() or "Deposit")
                persist()
                st.rerun()
            except Exception as e:
                fail(e)
    with cw, st.form("withdraw_form"):
        wd_amt = st.number_input("Withdraw $", min_value=0.0, value=0.0, step=100.0, format="%.2f")
        wd_reason = st.text_input("Note", value="Withdrawal", key="wd_note")
        if st.form_submit_button("Withdraw"):
            try:
                core.withdraw(S, hist, wd_amt, wd_reason.strip() or "Withdrawal")
                persist()
                st.rerun()
            except Exception as e:
                fail(e)
    st.caption("Deposits and withdrawals are dated at the latest close. The benchmark receives the same cash on the "
               "same date, and returns are time-weighted, so adding money never counts as performance.")

    st.subheader("Place an order")
    with st.form("order_form"):
        c1, c2, c3, c4 = st.columns(4)
        o_ticker = c1.text_input("Ticker").strip().upper()
        o_side = c2.selectbox("Side", ["BUY", "SELL"])
        o_type = c3.selectbox("Order type", ["MARKET", "LIMIT"])
        o_tif = c4.selectbox("Time in force (limit orders)", ["GTC", "DAY"])
        c5, c6, c7, c8 = st.columns(4)
        o_mode = c5.selectbox("Size by", ["Dollars", "Shares"])
        o_qty = c6.number_input("Amount (dollars or shares)", min_value=0.0, value=0.0, step=100.0, format="%.4f")
        o_limit = c7.number_input("Limit price (limit orders)", min_value=0.0, value=0.0, step=1.0, format="%.4f")
        o_exp = c8.text_input("Cancel after YYYY-MM-DD (optional)")
        o_reason = st.text_input("Why (required)")
        if st.form_submit_button("Submit order", type="primary"):
            exp = o_exp.strip() or None
            try:
                if exp:
                    dt.date.fromisoformat(exp)
                if not o_ticker or not o_reason.strip() or o_qty <= 0:
                    raise ValueError("Ticker, amount, and reason are required.")
                core.ensure_ticker(S, o_ticker)
                th = core.fetch(sorted(set(S["tickers"]) | {core.BENCH}), S["start_date"])
                core.check_splits(th, S["start_date"])
                order = core.place_order(
                    S, th, o_ticker, o_side, o_type, o_reason.strip(),
                    dollars=o_qty if o_mode == "Dollars" else None,
                    shares=o_qty if o_mode == "Shares" else None,
                    limit=o_limit or None, tif=o_tif, expires=exp)
                persist()
                st.rerun()
            except Exception as e:
                fail(e)
    st.caption("Market orders fill now at the latest close, worse by half the spread plus slippage, and pay "
               "commission, plus the SEC and FINRA fees on sells. After the market closes this is the last close, "
               "not a live quote. Limit orders wait for a later daily bar that trades through the limit and fill with no spread.")

    open_orders = [o for o in S["orders"] if o["status"] == "OPEN"]
    if open_orders:
        st.subheader("Open orders")
        st.dataframe(pd.DataFrame(open_orders)[["id", "placed", "ticker", "side", "type", "dollars", "shares",
                                                  "limit", "tif", "expires", "reason"]],
                     hide_index=True, use_container_width=True)
        with st.form("cancel_form"):
            oid = st.selectbox("Order to cancel", [o["id"] for o in open_orders])
            if st.form_submit_button("Cancel order"):
                try:
                    core.cancel_order(S, oid)
                    persist()
                    st.rerun()
                except Exception as e:
                    fail(e)
    if S["orders"]:
        st.subheader("Order history")
        st.dataframe(pd.DataFrame(S["orders"][::-1])[["id", "placed", "ticker", "side", "type", "dollars", "shares",
                                                        "limit", "tif", "status", "fill_date", "fill_price", "note", "reason"]],
                     hide_index=True, use_container_width=True)

    st.subheader("Trade and dividend log")
    rows = []
    for e in sorted(S["events"], key=lambda e: e["date"], reverse=True):
        if e["kind"] == "CASH":
            continue
        fd = e.get("fee_detail", {})
        rows.append([e["date"], e["kind"], e["ticker"], round(e["shares"], 4), round(e["price"], 2),
                     round(e["cash"], 2), round(e["fee"], 2), round(fd.get("sec", 0.0), 2),
                     round(fd.get("taf", 0.0), 2), e.get("order", ""), e["reason"]])
    st.dataframe(pd.DataFrame(rows, columns=["Date", "Type", "Ticker", "Shares", "Price", "Cash effect", "Fees",
                                             "of which SEC", "of which TAF", "Order", "Reason"]),
                 hide_index=True, use_container_width=True)

# ---------- Settings ----------
FLOAT_KEYS = ["spread_pct", "slippage_pct", "fee", "fee_per_share", "sec_fee_per_million",
              "taf_per_share", "taf_max", "tax_rate"]


def same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-12)
    return a == b


with tab_settings:
    now = core.today()
    gs = lambda k, d: core.get_setting(S, k, now, d)
    st.caption("Changes apply from today forward. Past trades keep the settings they were made under. "
               "A setting can also be scheduled for a later date through the Settings history log.")
    with st.form("settings_form"):
        c1, c2 = st.columns(2)
        spread = c1.slider("Bid-ask spread % (full spread)", 0.0, 1.0, float(gs("spread_pct", 0.05)), 0.01)
        slip = c2.slider("Extra slippage % per side (market orders)", 0.0, 1.0, float(gs("slippage_pct", 0.01)), 0.01)
        c3, c4 = st.columns(2)
        fee = c3.number_input("Commission $ per trade", min_value=0.0, value=float(gs("fee", 0.0)), format="%.4f")
        fps = c4.number_input("Commission $ per share", min_value=0.0, value=float(gs("fee_per_share", 0.0)), format="%.6f")
        st.markdown("Regulatory fees charged on sells and passed through by most brokers")
        c5, c6, c7 = st.columns(3)
        sec = c5.number_input("SEC fee $ per $1M sold", min_value=0.0, value=float(gs("sec_fee_per_million", 20.60)), format="%.4f")
        taf = c6.number_input("FINRA TAF $ per share sold", min_value=0.0, value=float(gs("taf_per_share", 0.0)), format="%.6f")
        tafmax = c7.number_input("FINRA TAF max $ per trade", min_value=0.0, value=float(gs("taf_max", 9.79)), format="%.2f")
        tax = st.slider("Estimated tax %", 0.0, 50.0, float(gs("tax_rate", 15.0)), 0.5)
        frac = st.checkbox("Fractional shares", value=bool(gs("fractional", True)))
        opts = ["Manual", "Monthly", "Quarterly"]
        reb = st.selectbox("Rebalance note (no automatic trades run)", opts, index=opts.index(gs("rebalance", "Manual")))
        sreason = st.text_input("Reason for setting changes (logged)")
        if st.form_submit_button("Apply settings", type="primary"):
            if not sreason.strip():
                st.error("Give a reason for setting changes.")
            else:
                new = {"spread_pct": spread, "slippage_pct": slip, "fee": fee, "fee_per_share": fps,
                       "sec_fee_per_million": sec, "taf_per_share": taf, "taf_max": tafmax,
                       "tax_rate": tax, "fractional": frac, "rebalance": reb}
                changed = False
                for k, v in new.items():
                    if not same(core.get_setting(S, k, now), v):
                        core.log_setting(S, k, v, now, sreason.strip())
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
            S["theses"].append({"added": core.today(), "claim": claim.strip(), "target": target.strip(),
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
    rules = st.text_area("Rules you are testing against", value=S["rules_text"], height=260)
    if st.button("Save rules"):
        S["rules_text"] = rules
        persist()
        st.success("Saved.")
    with st.form("dev_form"):
        dev = st.text_input("Deviation from the rules: what and why")
        if st.form_submit_button("Log deviation") and dev.strip():
            S["deviations"].append({"date": core.today(), "reason": dev.strip()})
            persist()
            st.rerun()
    st.dataframe(pd.DataFrame(S["deviations"]) if S["deviations"] else pd.DataFrame(columns=["date", "reason"]),
                 hide_index=True, use_container_width=True)

# ---------- Settings history ----------
with tab_hist:
    st.dataframe(pd.DataFrame([{"effective": x["effective"], "key": x["key"], "value": str(x["value"]),
                                "logged_at": x["logged_at"], "reason": x["reason"]} for x in S["settings"]]),
                 hide_index=True, use_container_width=True)
