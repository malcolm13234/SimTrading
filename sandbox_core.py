# =====================================================================
#  INVESTING SANDBOX  (Google Colab, ipywidgets)
#  Paste this whole file into ONE cell and run it.
#  Prices come from yfinance. If a price is missing or a stock split
#  occurs, the app stops and says so. It never fills in a made-up price.
# =====================================================================
import os, math, json, html, datetime as dt
import pandas as pd
import matplotlib.pyplot as plt
import yfinance as yf

# ---------- CONFIG (used only when no saved state exists) ----------
START_DOLLARS = {"SPY": 17000.0}     # opening positions, in dollars
START_CASH = 6000.0
BENCH = "SPY"                        # benchmark: S&P 500 total return, dividends reinvested
RULES_DEFAULT = """Benchmark: SPY total return, dividends reinvested, same start date and same starting dollars.
Costs: spread and fee settings apply to every trade from the day they are logged forward. Taxes are estimated at the rate in effect on the sale date.
Rebalancing: manual, using the Rebalance button. The Monthly/Quarterly setting is a note only; no automatic trades run.
Pass: beat the benchmark net of spreads, fees, and estimated taxes over the period stated in the thesis log.
Prices: trades fill at the latest close that yfinance returns, which can be intraday during market hours. Dividends are reinvested at the close on the ex-date.
Universe: only the tickers you add yourself. A delisted holding will stop pricing and the app will stop."""
TODAY = dt.date.today().isoformat()
STATE_PATH = None
STATE = None
LAST = {}

import matplotlib
matplotlib.use('Agg')
plt.style.use('dark_background')

# ---------- settings with dates ----------
def log_setting(s, key, value, effective, reason):
    s["settings"].append({"key": key, "value": value, "effective": effective,
                          "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
                          "reason": reason})


def get_setting(s, key, date, default=None):
    """Latest logged value whose effective date is on or before `date`."""
    val = default
    for x in s["settings"]:
        if x["key"] == key and x["effective"] <= date:
            val = x["value"]
    return val


def new_state():
    s = {"opening": dict(START_DOLLARS), "cash": START_CASH, "opened": False,
         "start_date": TODAY, "tickers": sorted({BENCH, *START_DOLLARS}),
         "events": [], "settings": [], "targets": {}, "theses": [], "deviations": [],
         "rules_text": RULES_DEFAULT, "last_div_date": TODAY}
    for k, v, why in [("spread_pct", 0.05, "Default: 0.05% full bid-ask spread"),
                      ("fee", 0.0, "Default: $0 commission"),
                      ("tax_rate", 15.0, "Default: 15% estimated tax on gains"),
                      ("fractional", True, "Default: fractional shares on"),
                      ("rebalance", "Manual", "Default: manual rebalancing")]:
        log_setting(s, k, v, TODAY, why)
        s["settings"][-1]["default"] = True
    for t in START_DOLLARS:
        log_setting(s, f"reinvest_{t}", 100.0, TODAY, "Default: reinvest 100%")
        s["settings"][-1]["default"] = True
    return s


# ---------- events ----------
def make_event(date, kind, ticker, shares, price, cash, reason, fee=0.0, cost=0.0, tax_rate=0.0):
    return {"date": date, "kind": kind, "ticker": ticker, "shares": float(shares),
            "price": float(price), "cash": float(cash), "fee": float(fee),
            "cost": float(cost), "tax_rate": float(tax_rate), "reason": reason}


def position(s, t, before):
    return sum(e["shares"] for e in s["events"] if e["ticker"] == t and e["date"] < before)


def cash_now(s):
    return sum(e["cash"] for e in s["events"])


# ---------- prices ----------
def fetch(tickers, start):
    hist = {}
    s_ = (pd.Timestamp(start) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    e_ = (pd.Timestamp.today().normalize() + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    for t in tickers:
        h = yf.Ticker(t).history(start=s_, end=e_, auto_adjust=False, actions=True)
        if h is None or h.empty:
            raise RuntimeError(f"No price data returned for {t}. Check the ticker symbol.")
        idx = pd.DatetimeIndex(h.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        h = h.copy()
        h.index = idx.normalize()
        hist[t] = h[["Close", "Dividends", "Stock Splits"]]
    return hist


def check_splits(hist, start):
    for t, h in hist.items():
        sp = h["Stock Splits"]
        sp = sp[(sp > 0) & (sp != 1) & (sp.index >= pd.Timestamp(start))]
        if len(sp):
            raise RuntimeError(f"{t} split on {sp.index[0].date()}. This app does not adjust "
                               f"share counts for splits. Use another ticker or adjust by hand.")


def last_close(hist, t):
    c = hist[t]["Close"].dropna()
    return float(c.iloc[-1]), c.index[-1].date().isoformat()


def close_on(hist, t, ds):
    c = hist[t]["Close"].dropna()
    c = c[c.index <= pd.Timestamp(ds)]
    if c.empty:
        raise RuntimeError(f"No {t} price on or before {ds}.")
    return float(c.iloc[-1])


def build_closes(hist, cal):
    df = pd.DataFrame({t: hist[t]["Close"] for t in hist})
    return df.reindex(df.index.union(cal)).ffill().reindex(cal)


# ---------- opening, dividends, trades ----------
def open_portfolio(s, hist):
    idx = hist[BENCH].index
    first = idx[idx >= pd.Timestamp(s["start_date"])]
    if len(first):
        d0 = first[0].date().isoformat()
    else:
        d0 = idx[-1].date().isoformat()
        print(f"Start date is after the latest data. Opening on {d0} instead.")
    s["start_date"] = d0
    for x in s["settings"]:              # default settings take effect on the real start date
        if x.get("default"):
            x["effective"] = d0
    s["events"].append(make_event(d0, "CASH", "CASH", 0, 1.0, s["cash"], "Opening cash"))
    for t, dollars in s["opening"].items():
        px = close_on(hist, t, d0)
        s["events"].append(make_event(d0, "OPEN", t, dollars / px, px, 0.0, "Opening position"))
    s["last_div_date"] = d0
    s["opened"] = True


def process_dividends(s, hist, cal):
    end = cal[-1].date().isoformat()
    todo = []
    for t in s["tickers"]:
        if t not in hist:
            continue
        d = hist[t]["Dividends"]
        for ts, amt in d[d > 0].items():
            ds = ts.date().isoformat()
            if s["last_div_date"] < ds <= end:
                todo.append((ds, t, float(amt)))
    for ds, t, amt in sorted(todo):
        sh = position(s, t, ds)          # shares held before the ex-date
        if sh <= 0:
            continue
        gross = sh * amt
        s["events"].append(make_event(ds, "DIVIDEND", t, 0, amt, gross, f"Dividend on {sh:.4f} sh"))
        pct = get_setting(s, f"reinvest_{t}", ds, 0.0)
        to_buy = gross * pct / 100
        if to_buy > 0:
            px = close_on(hist, t, ds)
            shares = to_buy / px
            if not get_setting(s, "fractional", ds, True):
                shares = math.floor(shares)
            if shares > 0:
                s["events"].append(make_event(ds, "DRIP", t, shares, px, -shares * px,
                                              f"Reinvest {pct:.0f}% of dividend"))
    s["last_div_date"] = end


def ensure_ticker(s, t):
    """Check the ticker has data, then add it. Returns True if added or already present."""
    if t in s["tickers"]:
        return True
    fetch([t], s["start_date"])          # raises if the ticker is bad
    s["tickers"].append(t)
    s["tickers"].sort()
    log_setting(s, f"reinvest_{t}", 0.0, TODAY, "Default: no reinvestment for new holding")
    return True


def place_trade(s, hist, t, side, dollars, reason):
    if t not in hist:
        raise RuntimeError(f"Refresh first: no price data for {t}.")
    px_mkt, d = last_close(hist, t)
    frac = get_setting(s, "fractional", d, True)
    half = get_setting(s, "spread_pct", d, 0.0) / 200      # half the full spread, per side
    fee = get_setting(s, "fee", d, 0.0)
    rate = get_setting(s, "tax_rate", d, 0.0)
    if side == "BUY":
        px = px_mkt * (1 + half)
        sh = dollars / px
        sh = sh if frac else math.floor(sh)
        if sh <= 0:
            raise ValueError("Amount too small to buy one share.")
        cash = -(sh * px + fee)
        if cash_now(s) + cash < -0.005:
            raise ValueError("Not enough cash for this buy.")
        ev = make_event(d, "BUY", t, sh, px, cash, reason, fee, abs(sh) * (px - px_mkt) + fee, rate)
    else:
        held = position(s, t, "9999-12-31")
        px = px_mkt * (1 - half)
        sh = min(dollars / px, held)
        sh = sh if frac else math.floor(sh)
        if sh <= 0:
            raise ValueError(f"Nothing to sell of {t}.")
        ev = make_event(d, "SELL", t, -sh, px, sh * px - fee, reason, fee,
                        abs(sh) * (px_mkt - px) + fee, rate)
    s["events"].append(ev)
    return ev


def rebalance(s, hist, r, reason):
    targets = {t: w for t, w in s["targets"].items() if w > 0}
    if sum(targets.values()) > 1.0001:
        raise ValueError("Target weights add up to more than 100%.")
    net = float(r["net"].iloc[-1])
    deltas = {}
    for t in s["tickers"]:
        mv = position(s, t, "9999-12-31") * last_close(hist, t)[0]
        deltas[t] = targets.get(t, 0.0) * net - mv
    for t, dlt in sorted(deltas.items(), key=lambda kv: kv[1]):
        if dlt < -1:
            place_trade(s, hist, t, "SELL", -dlt, reason)
    for t, dlt in sorted(deltas.items(), key=lambda kv: -kv[1]):
        if dlt > 1:
            amount = min(dlt, cash_now(s))
            if amount > 1:
                place_trade(s, hist, t, "BUY", amount, reason)


# ---------- replay and benchmark ----------
def replay(s, closes):
    evs = sorted(s["events"], key=lambda e: e["date"])   # stable sort keeps same-day order
    pos, avg, rows = {}, {}, []
    cash = liab = 0.0
    i = 0
    for day in closes.index:
        ds = day.date().isoformat()
        while i < len(evs) and evs[i]["date"] <= ds:
            e = evs[i]
            i += 1
            t, chg = e["ticker"], e["shares"]
            cash += e["cash"]
            if chg > 0:
                old, a = pos.get(t, 0.0), avg.get(t, 0.0)
                new = old + chg
                avg[t] = (a * old + chg * e["price"] + e["fee"]) / new
                pos[t] = new
            elif chg < 0:
                sold = -chg
                gain = sold * (e["price"] - avg.get(t, 0.0)) - e["fee"]
                liab = max(0.0, liab + e["tax_rate"] / 100 * gain)
                pos[t] = pos.get(t, 0.0) - sold
                if pos[t] <= 1e-9:
                    pos.pop(t, None)
                    avg.pop(t, None)
        mv = sum(sh * closes.at[day, t] for t, sh in pos.items())
        rows.append((day, cash + mv - liab, cash, mv, liab))
    return pd.DataFrame(rows, columns=["date", "net", "cash", "mv", "liability"]).set_index("date")


def benchmark(hist, closes, amount):
    c = closes[BENCH]
    d = hist[BENCH]["Dividends"].reindex(closes.index).fillna(0.0)
    sh = amount / c.iloc[0]
    out = []
    for day in closes.index:
        if d[day] > 0:
            sh += sh * d[day] / c[day]
        out.append(sh * c[day])
    return pd.Series(out, index=closes.index, name="benchmark")


def refresh(s):
    tickers = sorted(set(s["tickers"]) | {BENCH})
    hist = fetch(tickers, s["start_date"])
    if not s["opened"]:
        open_portfolio(s, hist)
    check_splits(hist, s["start_date"])
    idx = hist[BENCH].index
    cal = idx[idx >= pd.Timestamp(s["start_date"])]
    process_dividends(s, hist, cal)
    closes = build_closes(hist, cal)
    r = replay(s, closes)
    bench = benchmark(hist, closes, s["cash"] + sum(s["opening"].values()))
    return hist, r, bench


# ---------- reporting ----------
def metrics(s, r, bench):
    start_value = s["cash"] + sum(s["opening"].values())
    net = r["net"]
    trades = [e for e in s["events"] if e["kind"] in ("BUY", "SELL")]
    traded = sum(abs(e["shares"] * e["price"]) for e in trades)
    return [
        ("Start value", f"${start_value:,.2f}"),
        ("Sandbox net value", f"${net.iloc[-1]:,.2f}"),
        ("Sandbox return (net of costs and est. taxes)", f"{(net.iloc[-1] / start_value - 1) * 100:.2f}%"),
        ("Benchmark return (SPY total return)", f"{(bench.iloc[-1] / bench.iloc[0] - 1) * 100:.2f}%"),
        ("Excess return vs benchmark", f"{((net.iloc[-1] / start_value) - (bench.iloc[-1] / bench.iloc[0])) * 100:.2f} pts"),
        ("Max drawdown (sandbox)", f"{(net / net.cummax() - 1).min() * 100:.2f}%"),
        ("Estimated tax liability", f"${r['liability'].iloc[-1]:,.2f}"),
        ("Trades / dollars traded", f"{len(trades)} / ${traded:,.2f}"),
        ("Turnover (dollars traded / avg value)", f"{traded / net.mean():.2f}x"),
        ("Total costs logged", f"${sum(e['cost'] for e in s['events']):,.2f}"),
    ]


def segment_rows(s, r, bench):
    first, last = r.index[0], r.index[-1]
    cuts = sorted({x["effective"] for x in s["settings"] if s["start_date"] < x["effective"] <= last.date().isoformat()})
    pts = [first] + [pd.Timestamp(c) for c in cuts] + [last]
    rows = []
    for a, b in zip(pts[:-1], pts[1:]):
        sr = r["net"].asof(b) / r["net"].asof(a) - 1
        br = bench.asof(b) / bench.asof(a) - 1
        rows.append((a.date(), b.date(), f"{sr * 100:.2f}%", f"{br * 100:.2f}%", f"{(sr - br) * 100:.2f} pts"))
    return rows


def draw_chart(s, r, bench):
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    base = r["net"].iloc[0]
    ax[0].plot(r.index, r["net"], color="#4fc3f7", label="Sandbox (net)")
    ax[0].plot(bench.index, bench, color="#ffb74d", label="SPY total return")
    ax[0].set_title("Value ($)")
    ax[0].legend(fontsize=8)
    ax[1].plot(r.index, (r["net"] / base - 1) * 100, color="#4fc3f7", label="Sandbox")
    ax[1].plot(bench.index, (bench / bench.iloc[0] - 1) * 100, color="#ffb74d", label="SPY")
    ax[1].set_title("Return since start (%)")
    cuts = {x["effective"] for x in s["settings"] if x["effective"] > s["start_date"]}
    for c in cuts:
        for a in ax:
            a.axvline(pd.Timestamp(c), color="#777", ls="--", lw=0.8)
    for a in ax:
        a.grid(True, alpha=0.25)
        a.spines[["top", "right"]].set_visible(False)
        a.legend(fontsize=9, frameon=False) if a.get_legend_handles_labels()[0] else None
    fig.tight_layout()
    plt.show()


def holdings_rows(s, hist):
    total_mv, rows = 0.0, []
    tickers = sorted(set(s["tickers"]))
    for t in tickers:
        sh = position(s, t, "9999-12-31")
        if sh <= 1e-9:
            continue
        px, _ = last_close(hist, t)
        mv = sh * px
        total_mv += mv
        rows.append([t, f"{sh:,.4f}", f"{px:,.2f}", f"${mv:,.2f}",
                     f"{get_setting(s, f'reinvest_{t}', TODAY, 0.0):.0f}%",
                     f"{s['targets'].get(t, 0.0) * 100:.1f}%"])
    cash = cash_now(s)
    for row in rows:
        row.insert(4, f"{float(row[3].strip('$').replace(',', '')) / (total_mv + cash) * 100:.1f}%")
    rows.append(["CASH", "", "", f"${cash:,.2f}", f"{cash / (total_mv + cash) * 100:.1f}%", "", ""])
    return rows


