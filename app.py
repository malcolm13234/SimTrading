import json
import base64
import datetime as dt
import importlib
import math
import pandas as pd
import requests
import streamlit as st

import sandbox_core as core

if not hasattr(core, "fetch_detail"):   # a pre-update module stayed in memory across the redeploy
    importlib.reload(core)
    if hasattr(core, "_raw_fetch"):
        del core._raw_fetch

import charts

st.set_page_config(page_title="SimTrading", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

UP, DOWN = charts.UP, charts.DOWN

st.markdown("""
<style>
.block-container {max-width: 1180px; padding-top: 2.2rem;}
#MainMenu, footer {visibility: hidden;}
.hero-label {color:#8b9099; font-size:.95rem; margin-bottom:.1rem;}
.hero-value {font-size:2.7rem; font-weight:600; letter-spacing:-0.025em; line-height:1.1;}
.hero-sub {font-size:1rem; margin-top:.3rem;}
.up {color:#00C805;} .down {color:#FF5000;} .muted {color:#8b9099;}
.stat {display:flex; justify-content:space-between; padding:.55rem 0; border-bottom:1px solid rgba(255,255,255,.07); font-size:.92rem;}
.stat span:first-child {color:#8b9099;}
.card {background:#14161b; border:1px solid rgba(255,255,255,.06); border-radius:14px; padding:1rem 1.25rem; margin-bottom:.8rem;}
.ticker-name {font-size:1.9rem; font-weight:600; letter-spacing:-0.02em;}
div[data-testid="stMetric"] {background:#14161b; border:1px solid rgba(255,255,255,.06); border-radius:14px; padding:.85rem 1.1rem;}
div[data-testid="stMetricLabel"] {color:#8b9099;}
button[kind="primary"], button[kind="primaryFormSubmit"] {border-radius:999px; font-weight:600;}
.stTabs [data-baseweb="tab-list"] {gap:1.6rem; border-bottom:1px solid rgba(255,255,255,.07);}
.stTabs [data-baseweb="tab"] {padding-left:0; padding-right:0;}
h2, h3 {letter-spacing:-0.015em;}
</style>
""", unsafe_allow_html=True)


# The core module outlives script reruns, so capture the real fetch only once. Without this each
# rerun wrapped the previous run's cached wrapper, stacking caches and deadlocking on a cache miss.
if not hasattr(core, "_raw_fetch"):
    core._raw_fetch = core.fetch
_orig_fetch = core._raw_fetch


@st.cache_data(ttl=900, show_spinner=False)
def cached_fetch_v2(tickers, start):   # new name so cached results from the old version are never reused
    return _orig_fetch(tuple(tickers), start)


core.fetch = cached_fetch_v2  # core.refresh and the order code use the cached price fetch


@st.cache_data(ttl=3600, show_spinner=False)
def cached_detail(t):
    return core.fetch_detail(t)


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def cached_info(t):
    return core.fetch_info(t)


@st.cache_data(ttl=1800, show_spinner=False)
def cached_news(t):
    return core.fetch_news(t)


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
        st.session_state["sync_error"] = f"Auto-sync failed, changes are only in this session: {e}"


def persist_market():
    if not sync_on():
        return
    try:
        st.session_state["sha_market"] = gh_put(GH_MARKET, st.session_state.market, st.session_state.get("sha_market"),
                                                f"Market data through {core.today()}", compact=True)
    except Exception as e:
        st.session_state["sync_error"] = f"Market data sync failed: {e}"


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

with st.sidebar:
    st.caption(f"Syncing to {gh_repo()}" if sync_on() else
               "Not syncing: no secret named github_token. Secret names this app can see: "
               f"{secret_names() or 'none'}. Add github_token in the app's Settings, Secrets.")
    if st.session_state.get("sync_error"):
        st.error(st.session_state["sync_error"])
    st.download_button("Download state (.json)", data=json.dumps(S, indent=1),
                       file_name="sandbox_state.json", mime="application/json")
    st.download_button("Download market data (.json)", data=json.dumps(M, separators=(",", ":")),
                       file_name="market_data.json", mime="application/json")


# ---------- small helpers ----------
def money(x, signed=False):
    s = f"${abs(x):,.2f}"
    if signed:
        return ("+" if x >= 0 else "-") + s
    return ("-" if x < 0 else "") + s


def pct(x, signed=True):
    return f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%"


def tone(x):
    return "up" if x >= 0 else "down"


def delta_html(amount, ratio, suffix=""):
    return f'<span class="{tone(amount)}">{money(amount, True)} ({pct(ratio)})</span> <span class="muted">{suffix}</span>'


def human(n):
    if n is None or (isinstance(n, float) and math.isnan(n)):
        return "—"
    n = float(n)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:,.2f}{unit}"
    return f"{n:,.0f}"


def stat_block(pairs):
    st.markdown("".join(f'<div class="stat"><span>{k}</span><span>{v}</span></div>' for k, v in pairs),
                unsafe_allow_html=True)


net, flow = r["net"], r["flow"]
contributed = float(flow.sum())
profit = float(net.iloc[-1]) - contributed
if len(net) > 1:
    prev = float(net.iloc[-2])
    day_chg = float(net.iloc[-1]) - prev - float(flow.iloc[-1])
    day_pct = day_chg / prev if prev > 0 else 0.0
else:
    day_chg, day_pct = 0.0, 0.0
cash = core.cash_now(S)
tickers = sorted(set(S["tickers"]))


def quote(t):
    c = hist[t]["Close"].dropna()
    last = float(c.iloc[-1])
    prev_c = float(c.iloc[-2]) if len(c) > 1 else last
    return last, (last / prev_c - 1) if prev_c else 0.0


def spark(t):
    try:
        d = cached_detail(t)["Close"].tail(30)
    except Exception:
        d = hist[t]["Close"].dropna().tail(30)
    return [float(x) for x in d]


tab_home, tab_stocks, tab_trade, tab_activity, tab_settings, tab_journal = st.tabs(
    ["Home", "Stocks", "Trade", "Activity", "Settings", "Journal"])

# =====================================================================
# HOME
# =====================================================================
with tab_home:
    st.markdown(f'<div class="hero-label">Total value</div><div class="hero-value">{money(float(net.iloc[-1]))}</div>'
                f'<div class="hero-sub">{delta_html(day_chg, day_pct, "today")}</div>'
                f'<div class="hero-sub">{delta_html(profit, profit / contributed if contributed else 0.0, "all time")}</div>',
                unsafe_allow_html=True)
    rng = st.segmented_control("Range", charts.RANGES, default="ALL", key="home_range", label_visibility="collapsed") or "ALL"
    show_bench = st.toggle("Compare with SPY, same cash flows", value=True, key="home_bench")
    st.plotly_chart(charts.portfolio_fig(net, bench["value"], r["twr"], rng, show_bench),
                    width="stretch", config={"displayModeBar": False}, key="home_chart")
    if len(net) < 5:
        st.caption("The account opened recently, so the chart fills in as trading days pass.")

    m1, m2, m3, m4 = st.columns(4)
    sb, bb = float(r["twr"].iloc[-1] - 1), float(bench["twr"].iloc[-1] - 1)
    m1.metric("Buying power", money(cash))
    m2.metric("Money put in", money(contributed))
    m3.metric("Return vs SPY", pct(sb), f"{(sb - bb) * 100:+.2f} pts vs SPY")
    m4.metric("Est. tax owed", money(float(r["liability"].iloc[-1])))

    left, right = st.columns([2, 3], gap="large")
    with left:
        st.subheader("Allocation")
        labels, values = [], []
        for t in tickers:
            v = core.position(S, t, "9999-12-31") * quote(t)[0]
            if v > 0.005:
                labels.append(t)
                values.append(v)
        if cash > 0.005:
            labels.append("Cash")
            values.append(cash)
        if values:
            st.plotly_chart(charts.allocation_fig(labels, values), width="stretch",
                            config={"displayModeBar": False}, key="alloc_chart")
    with right:
        st.subheader("Performance details")
        stat_block(core.metrics(S, r, bench))
        with st.expander("Settings segments"):
            st.caption("Each segment uses the settings in force during it. Compare segments of similar length.")
            st.dataframe(pd.DataFrame(core.segment_rows(S, r, bench),
                                      columns=["From", "To", "Sandbox", "SPY total return", "Difference"]),
                         hide_index=True, width="stretch")


# =====================================================================
# STOCKS
# =====================================================================
def set_drip(t):
    v = float(st.session_state[f"drip_{t}"])
    state = st.session_state.state
    if abs(core.get_setting(state, f"reinvest_{t}", core.today(), 0.0) - v) > 1e-9:
        core.log_setting(state, f"reinvest_{t}", v, core.today(), f"Slider set to {v:.0f}%")
        persist()


def render_stock(t):
    price, day = quote(t)
    try:
        df = cached_detail(t)
        full_hist = True
    except Exception:
        df, full_hist = hist[t].dropna(subset=["Close"]), False
    info = cached_info(t)
    name = info.get("longName") or info.get("shortName") or ""
    st.markdown(f'<div class="ticker-name">{t} <span class="muted" style="font-size:1rem;font-weight:400">{name}</span></div>',
                unsafe_allow_html=True)
    day_d = price * day / (1 + day) if day > -1 else 0.0
    st.markdown(f'<div class="hero-value" style="font-size:2.2rem">{money(price)}</div>'
                f'<div class="hero-sub">{delta_html(day_d, day, "today")}</div>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns([5, 2, 2])
    srng = c1.segmented_control("Range", [x for x in charts.RANGES if x != "ALL"] + ["ALL"], default="1Y",
                                key=f"srng_{t}", label_visibility="collapsed") or "1Y"
    kind = c2.segmented_control("Chart", ["Line", "Candles"], default="Line", key=f"kind_{t}",
                                label_visibility="collapsed") or "Line"
    ma = c3.toggle("50-day average", key=f"ma_{t}")
    trades = [(e["date"], "BUY" if e["shares"] > 0 else "SELL", e["price"]) for e in S["events"]
              if e["ticker"] == t and e["kind"] in ("BUY", "SELL", "OPEN")]
    st.plotly_chart(charts.stock_fig(df, trades, srng, kind, ma), width="stretch",
                    config={"displayModeBar": False}, key=f"stock_chart_{t}")
    if not full_hist:
        st.caption("Full price history could not be loaded, so this chart only covers the days since the account opened.")

    last_bar = df.iloc[-1]
    prev_close = float(df["Close"].iloc[-2]) if len(df) > 1 else float(last_bar["Close"])
    year = df.tail(252)
    ttm = float(df["Dividends"][df.index >= df.index[-1] - pd.Timedelta(days=365)].sum())
    a, b = st.columns(2, gap="large")
    with a:
        st.subheader("Key stats")
        stat_block([("Open", money(float(last_bar["Open"]))), ("High", money(float(last_bar["High"]))),
                    ("Low", money(float(last_bar["Low"]))), ("Previous close", money(prev_close)),
                    ("Volume", human(last_bar["Volume"])), ("Avg volume (30 days)", human(df["Volume"].tail(30).mean())),
                    ("52-week high", money(float(year["High"].max()))), ("52-week low", money(float(year["Low"].min())))])
    with b:
        st.subheader("About")
        stat_block([("Market cap", human(info.get("marketCap"))),
                    ("P/E (trailing)", f"{info['trailingPE']:.2f}" if info.get("trailingPE") else "—"),
                    ("Beta", f"{info['beta']:.2f}" if info.get("beta") else "—"),
                    ("Dividends, last 12 months", f"{money(ttm)} per share" if ttm else "None"),
                    ("Dividend yield", pct(ttm / price, False) if ttm and price else "—"),
                    ("Sector", info.get("sector") or "—"), ("Industry", info.get("industry") or "—")])

    ps = core.position_stats(S, t, price)
    st.subheader("Your position")
    if ps["shares"] > 1e-9:
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Shares", f"{ps['shares']:,.4f}")
        p2.metric("Market value", money(ps["value"]))
        p3.metric("Average cost", money(ps["avg_cost"]))
        p4.metric("Total return", money(ps["gain"], True), pct(ps["gain_pct"]))
        stat_block([("Dividends received", money(ps["dividends"])),
                    ("Projected dividends, next 12 months", money(ps["shares"] * ttm)),
                    ("Dividend reinvestment", f"{core.get_setting(S, f'reinvest_{t}', core.today(), 0.0):.0f}%")])
    else:
        st.caption(f"You don't own any {t}. Use the Trade tab to place an order.")

    divs = df["Dividends"][df["Dividends"] > 0]
    if len(divs):
        st.subheader("Dividend history")
        st.plotly_chart(charts.dividend_fig(divs), width="stretch", config={"displayModeBar": False}, key=f"div_{t}")

    if info.get("longBusinessSummary"):
        txt = info["longBusinessSummary"]
        st.markdown(f'<div class="card muted" style="font-size:.92rem">{txt[:520]}{"…" if len(txt) > 520 else ""}</div>',
                    unsafe_allow_html=True)
    news = cached_news(t)
    if news:
        st.subheader("News")
        for title, url, pub in news:
            st.markdown(f"[{title}]({url})  \n<span class='muted' style='font-size:.8rem'>{pub or ''}</span>",
                        unsafe_allow_html=True)


with tab_stocks:
    rows = []
    for t in tickers:
        px, d1 = quote(t)
        ps = core.position_stats(S, t, px)
        rows.append({"Stock": t, "30 days": spark(t), "Price": px, "Today": d1 * 100, "Shares": ps["shares"],
                     "Value": ps["value"], "Return": ps["gain_pct"] * 100 if ps["shares"] > 1e-9 else None,
                     "Reinvest": core.get_setting(S, f"reinvest_{t}", core.today(), 0.0)})
    table = pd.DataFrame(rows).sort_values("Value", ascending=False).reset_index(drop=True)
    total_val = float(table["Value"].sum()) + cash
    table["Weight"] = table["Value"] / total_val * 100 if total_val > 0 else 0.0
    table = table[["Stock", "30 days", "Price", "Today", "Shares", "Value", "Return", "Weight", "Reinvest"]]
    styled = table.style.map(lambda v: "" if v is None or pd.isna(v) else f"color: {UP if v >= 0 else DOWN}",
                             subset=["Today", "Return"])
    st.caption("Click a row to open the stock.")
    ev = st.dataframe(
        styled, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key="stock_table",
        column_config={
            "30 days": st.column_config.LineChartColumn("30 days", width="small"),
            "Price": st.column_config.NumberColumn(format="$%.2f"),
            "Today": st.column_config.NumberColumn(format="%+.2f%%"),
            "Shares": st.column_config.NumberColumn(format="%.4f"),
            "Value": st.column_config.NumberColumn(format="$%.2f"),
            "Return": st.column_config.NumberColumn(format="%+.2f%%"),
            "Weight": st.column_config.NumberColumn(format="%.1f%%"),
            "Reinvest": st.column_config.NumberColumn("Dividends reinvested", format="%.0f%%"),
        })
    picked = []
    try:
        picked = list(ev.selection.rows)
    except Exception:
        pass
    if picked and picked[0] < len(table):
        st.session_state["sel"] = table.iloc[picked[0]]["Stock"]
    sel = st.session_state.get("sel")
    if sel not in tickers:
        sel = table.iloc[0]["Stock"]

    with st.expander("Add a stock"):
        with st.form("add_ticker", clear_on_submit=True):
            new_t = st.text_input("Ticker, for example VOO or AAPL")
            if st.form_submit_button("Add stock") and new_t.strip():
                try:
                    core.ensure_ticker(S, new_t.strip().upper())
                    st.session_state["sel"] = new_t.strip().upper()
                    persist()
                    st.rerun()
                except Exception as e:
                    fail(e)

    st.divider()
    render_stock(sel)

    st.divider()
    st.subheader("Dividend reinvestment")
    st.caption("What share of each dividend buys more of that stock. Changes apply from today forward and are logged in Activity.")
    order = [sel] + [t for t in tickers if t != sel]
    cols = st.columns(2, gap="large")
    for i, t in enumerate(order):
        with cols[i % 2]:
            st.slider(f"{t}", 0.0, 100.0, float(core.get_setting(S, f"reinvest_{t}", core.today(), 0.0)), 1.0,
                      format="%d%%", key=f"drip_{t}", on_change=set_drip, args=(t,))

    with st.expander("Targets and rebalancing"):
        with st.form("targets_form"):
            targets = {}
            tcols = st.columns(3)
            for i, t in enumerate(tickers):
                targets[t] = tcols[i % 3].number_input(f"{t} target %", 0.0, 100.0, float(S["targets"].get(t, 0.0) * 100), 0.5,
                                                       key=f"tg_{t}")
            if st.form_submit_button("Save targets"):
                for t, v in targets.items():
                    if v > 0:
                        S["targets"][t] = v / 100
                    else:
                        S["targets"].pop(t, None)
                persist()
                st.rerun()
        if st.button("Rebalance to targets now"):
            try:
                rb_hist = core.fetch(sorted(set(S["tickers"]) | {core.BENCH}), S["start_date"])
                core.rebalance(S, rb_hist, r, "Rebalance to targets")
                persist()
                st.rerun()
            except Exception as e:
                fail(e)

# =====================================================================
# TRADE
# =====================================================================
with tab_trade:
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Trade")
        c1, c2 = st.columns([2, 3])
        o_ticker = c1.text_input("Ticker", value=st.session_state.get("sel") or tickers[0], key="o_ticker").strip().upper()
        o_side = (c2.segmented_control("Side", ["Buy", "Sell"], default="Buy", key="o_side") or "Buy").upper()
        c3, c4 = st.columns(2)
        o_type = (c3.segmented_control("Order type", ["Market", "Limit"], default="Market", key="o_type") or "Market").upper()
        o_mode = c4.segmented_control("Size by", ["Dollars", "Shares"], default="Dollars", key="o_mode") or "Dollars"
        c5, c6 = st.columns(2)
        o_qty = c5.number_input("Dollars" if o_mode == "Dollars" else "Shares", min_value=0.0, value=0.0,
                                step=100.0 if o_mode == "Dollars" else 1.0, format="%.4f", key="o_qty")
        o_limit = c6.number_input("Limit price (limit orders only)", min_value=0.0, value=0.0, step=1.0, format="%.4f", key="o_limit")
        c7, c8 = st.columns(2)
        o_tif = c7.selectbox("Good for", ["GTC", "DAY"], key="o_tif",
                             help="GTC stays open until filled or canceled. DAY expires after the next trading day.")
        o_exp = c8.text_input("Cancel after, YYYY-MM-DD (optional)", key="o_exp")
        o_reason = st.text_input("Why (required)", key="o_reason")

        est = None
        if o_ticker in hist and o_qty > 0:
            lp, ld = core.last_close(hist, o_ticker)
            est = core.estimate_order(S, lp, ld, o_side, o_type, o_qty if o_mode == "Dollars" else None,
                                      o_qty if o_mode == "Shares" else None, o_limit or None)
        elif o_ticker and o_ticker not in hist:
            st.caption(f"{o_ticker} isn't in your list yet. It will be added when you submit.")
        if est:
            fd = est["fee_detail"]
            stat_block([("Estimated fill price", money(est["price"])), ("Shares", f"{est['shares']:,.4f}"),
                        ("Commission", money(fd["commission"])), ("SEC fee", money(fd["sec"])),
                        ("FINRA TAF", money(fd["taf"])), ("Estimated cash effect", money(est["cash"], True)),
                        ("Cash after", money(cash + est["cash"]))])
        if st.button(f"Submit {o_side.lower()} order", type="primary", width="stretch", key="o_submit"):
            exp = o_exp.strip() or None
            try:
                if exp:
                    dt.date.fromisoformat(exp)
                if not o_ticker or not o_reason.strip() or o_qty <= 0:
                    raise ValueError("Ticker, amount, and reason are required.")
                core.ensure_ticker(S, o_ticker)
                th = core.fetch(sorted(set(S["tickers"]) | {core.BENCH}), S["start_date"])
                core.check_splits(th, S["start_date"])
                core.place_order(S, th, o_ticker, o_side, o_type, o_reason.strip(),
                                 dollars=o_qty if o_mode == "Dollars" else None,
                                 shares=o_qty if o_mode == "Shares" else None,
                                 limit=o_limit or None, tif=o_tif, expires=exp)
                persist()
                st.rerun()
            except Exception as e:
                fail(e)
        st.caption("Market orders fill at the latest close, worse by half the spread plus slippage. After the close "
                   "that is the last close, not a live quote. Limit orders wait for a later daily bar that trades "
                   "through the limit and fill with no spread. Sells also pay the SEC and FINRA fees.")

    with right:
        st.subheader("Cash")
        st.metric("Buying power", money(cash))
        with st.form("deposit_form", clear_on_submit=True):
            dep_amt = st.number_input("Deposit $ (no limit)", min_value=0.0, value=0.0, step=1000.0, format="%.2f")
            dep_note = st.text_input("Note", value="Deposit")
            if st.form_submit_button("Deposit", type="primary", width="stretch"):
                try:
                    core.deposit(S, hist, dep_amt, dep_note.strip() or "Deposit")
                    persist()
                    st.rerun()
                except Exception as e:
                    fail(e)
        with st.form("withdraw_form", clear_on_submit=True):
            wd_amt = st.number_input("Withdraw $", min_value=0.0, value=0.0, step=100.0, format="%.2f")
            wd_note = st.text_input("Note", value="Withdrawal")
            if st.form_submit_button("Withdraw", width="stretch"):
                try:
                    core.withdraw(S, hist, wd_amt, wd_note.strip() or "Withdrawal")
                    persist()
                    st.rerun()
                except Exception as e:
                    fail(e)
        st.caption("Deposits and withdrawals are dated at the latest close. The benchmark receives the same cash on the "
                   "same date, and returns are time-weighted, so adding money never counts as performance.")
        open_orders = [o for o in S["orders"] if o["status"] == "OPEN"]
        if open_orders:
            st.subheader("Open orders")
            st.dataframe(pd.DataFrame(open_orders)[["id", "ticker", "side", "type", "dollars", "shares", "limit", "tif", "expires"]],
                         hide_index=True, width="stretch")
            with st.form("cancel_form"):
                oid = st.selectbox("Order to cancel", [o["id"] for o in open_orders])
                if st.form_submit_button("Cancel order"):
                    try:
                        core.cancel_order(S, oid)
                        persist()
                        st.rerun()
                    except Exception as e:
                        fail(e)

# =====================================================================
# ACTIVITY
# =====================================================================
with tab_activity:
    a1, a2, a3 = st.tabs(["Trades and dividends", "Orders", "Settings history"])
    with a1:
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
                     hide_index=True, width="stretch")
    with a2:
        if S["orders"]:
            st.dataframe(pd.DataFrame(S["orders"][::-1])[["id", "placed", "ticker", "side", "type", "dollars", "shares",
                                                            "limit", "tif", "status", "fill_date", "fill_price", "note", "reason"]],
                         hide_index=True, width="stretch")
        else:
            st.caption("No orders yet.")
    with a3:
        st.dataframe(pd.DataFrame([{"effective": x["effective"], "key": x["key"], "value": str(x["value"]),
                                    "logged_at": x["logged_at"], "reason": x["reason"]} for x in S["settings"]]),
                     hide_index=True, width="stretch")

# =====================================================================
# SETTINGS
# =====================================================================
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
               "Dividend reinvestment sliders live on the Stocks tab.")
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

# =====================================================================
# JOURNAL
# =====================================================================
with tab_journal:
    j1, j2 = st.columns(2, gap="large")
    with j1:
        st.subheader("Thesis")
        with st.form("thesis_form", clear_on_submit=True):
            claim = st.text_input("Claim")
            target = st.text_input("Measurable target")
            deadline = st.text_input("Deadline YYYY-MM-DD")
            if st.form_submit_button("Add thesis") and claim.strip() and deadline.strip():
                S["theses"].append({"added": core.today(), "claim": claim.strip(), "target": target.strip(),
                                    "deadline": deadline.strip(), "outcome": "pending"})
                persist()
                st.rerun()
        if S["theses"]:
            with st.form("outcome_form", clear_on_submit=True):
                idx = st.selectbox("Thesis", range(len(S["theses"])),
                                   format_func=lambda i: f"{i + 1}. {S['theses'][i]['claim']}")
                outcome = st.text_input("Outcome and evidence")
                if st.form_submit_button("Record outcome") and outcome.strip():
                    S["theses"][idx]["outcome"] = outcome.strip()
                    persist()
                    st.rerun()
        st.dataframe(pd.DataFrame(S["theses"]) if S["theses"] else pd.DataFrame(
            columns=["added", "claim", "target", "deadline", "outcome"]), hide_index=True, width="stretch")
    with j2:
        st.subheader("Rules")
        rules = st.text_area("Rules you are testing against", value=S["rules_text"], height=260)
        if st.button("Save rules"):
            S["rules_text"] = rules
            persist()
            st.success("Saved.")
        with st.form("dev_form", clear_on_submit=True):
            dev = st.text_input("Deviation from the rules: what and why")
            if st.form_submit_button("Log deviation") and dev.strip():
                S["deviations"].append({"date": core.today(), "reason": dev.strip()})
                persist()
                st.rerun()
        st.dataframe(pd.DataFrame(S["deviations"]) if S["deviations"] else pd.DataFrame(columns=["date", "reason"]),
                     hide_index=True, width="stretch")
