# =====================================================================
#  SIMTRADING CORE
#  Prices come from yfinance. If a price is missing or a stock split
#  occurs, the app stops and says so. It never fills in a made-up price.
#
#  What this module does:
#   - deposits and withdrawals of any size (returns are time-weighted, so
#     adding cash does not look like performance; the benchmark gets the
#     same cash flows)
#   - market and limit orders with a realistic fee model: bid-ask spread,
#     slippage, flat and per-share commission, SEC Section 31 fee and FINRA
#     TAF on sells
#   - every setting is dated, logged with a reason, and applies going forward
#   - a record of the market data it used, kept separate from the state
# =====================================================================
import math, json, hashlib, datetime as dt
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import yfinance as yf

matplotlib.use("Agg")
plt.style.use("dark_background")

# ---------- CONFIG (used only when no saved state exists) ----------
START_DOLLARS = {"SPY": 17000.0}     # opening positions, in dollars
START_CASH = 6000.0
BENCH = "SPY"                        # benchmark: S&P 500 total return, dividends reinvested
RULES_DEFAULT = """Benchmark: SPY total return, dividends reinvested, receiving the same deposits and withdrawals on the same dates as the sandbox.
Returns: time-weighted, so deposits and withdrawals are not counted as performance.
Costs: spread, slippage, commission and regulatory-fee settings apply to every trade from the day they are logged forward. Taxes are estimated at the rate in effect on the sale date.
Orders: market orders fill at the latest close the data source returns, worse by half the spread plus slippage. Limit orders fill on a later daily bar only if price trades through the limit (low below a buy limit, high above a sell limit), at the limit or the open if it gapped past, with no spread. Open buy orders do not reserve cash; cash is checked when the order fills.
Rebalancing: manual, using the Rebalance button. The Monthly/Quarterly setting is a note only; no automatic trades run.
Pass: beat the benchmark net of spreads, fees, and estimated taxes over the period stated in the thesis log.
Dividends are reinvested at the close on the ex-date.
Universe: only the tickers you add yourself. A delisted holding will stop pricing and the app will stop."""


def today():
    return dt.date.today().isoformat()


# Settings added after the first version. Added to a state only if the key is absent.
# (key, value, effective date or None for the account start date, reason)
COST_DEFAULTS = [
    ("slippage_pct", 0.01, None, "Default: 0.01% extra market-order slippage per side"),
    ("fee_per_share", 0.0, None, "Default: $0 per-share commission"),
    ("sec_fee_per_million", 20.60, None,
     "SEC Section 31 fee on sales: $20.60 per $1M from 2026-04-04 (SEC Fee Rate Advisory, FY2026)"),
    ("taf_per_share", 0.0, None,
     "FINRA TAF paused 2026-10-01 to 2026-12-31 (SR-FINRA-2026-021)"),
    ("taf_per_share", 0.000195, "2027-01-01",
     "Scheduled: FINRA TAF resumes 2027-01-01 at $0.000195 per share (SR-FINRA-2026-021)"),
    ("taf_max", 9.79, None, "FINRA TAF maximum per trade, 2026 schedule"),
]
FLOW_KINDS = ("CASH", "DEPOSIT", "WITHDRAW")
MARKET_COLS = ["Open", "High", "Low", "Close", "Volume", "Dividends", "Stock Splits"]


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


def ensure_cost_settings(s):
    """Add the cost and regulatory-fee settings to a state that does not have them yet."""
    have = {x["key"] for x in s["settings"]}
    for key, val, eff, why in COST_DEFAULTS:
        if key in have:
            continue
        log_setting(s, key, val, eff or s["start_date"], why)
        if eff is None:
            s["settings"][-1]["default"] = True    # re-dated to the real start date when the account opens


def migrate(s):
    s.setdefault("orders", [])
    ensure_cost_settings(s)


def new_state():
    t0 = today()
    s = {"opening": dict(START_DOLLARS), "cash": START_CASH, "opened": False,
         "start_date": t0, "tickers": sorted({BENCH, *START_DOLLARS}),
         "events": [], "orders": [], "settings": [], "targets": {}, "theses": [], "deviations": [],
         "rules_text": RULES_DEFAULT, "last_div_date": t0}
    for k, v, why in [("spread_pct", 0.05, "Default: 0.05% full bid-ask spread"),
                      ("fee", 0.0, "Default: $0 commission per trade"),
                      ("tax_rate", 15.0, "Default: 15% estimated tax on gains"),
                      ("fractional", True, "Default: fractional shares on"),
                      ("rebalance", "Manual", "Default: manual rebalancing")]:
        log_setting(s, k, v, t0, why)
        s["settings"][-1]["default"] = True
    for t in START_DOLLARS:
        log_setting(s, f"reinvest_{t}", 100.0, t0, "Default: reinvest 100%")
        s["settings"][-1]["default"] = True
    ensure_cost_settings(s)
    return s


def signature(obj):
    return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


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
        hist[t] = h[MARKET_COLS]
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


# ---------- market data record ----------
def new_market():
    return {"source": "yfinance, unadjusted prices", "columns": MARKET_COLS, "updated": None, "prices": {}}


def merge_market(market, hist):
    """Add the bars we used to the record. Rows older than a week are never rewritten.
    Returns how many new dates were added."""
    cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    new = 0
    for t, h in hist.items():
        rows = market["prices"].setdefault(t, {})
        for ts, row in h.dropna(subset=["Close"]).iterrows():
            ds = ts.date().isoformat()
            if ds in rows and ds < cutoff:
                continue
            if ds not in rows:
                new += 1
            rows[ds] = [None if pd.isna(row.get(c)) else round(float(row.get(c)), 6) for c in MARKET_COLS]
    if new:
        market["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    return new


# ---------- opening, dividends ----------
def open_portfolio(s, hist):
    idx = hist[BENCH].index
    first = idx[idx >= pd.Timestamp(s["start_date"])]
    if len(first):
        d0 = first[0].date().isoformat()
    else:
        d0 = idx[-1].date().isoformat()
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
    log_setting(s, f"reinvest_{t}", 0.0, today(), "Default: no reinvestment for new holding")
    return True


# ---------- cash ----------
def deposit(s, hist, amount, reason="Deposit"):
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Deposit must be a positive amount.")
    d = last_close(hist, BENCH)[1]
    ev = make_event(d, "DEPOSIT", "CASH", 0, 1.0, amount, reason or "Deposit")
    s["events"].append(ev)
    return ev


def withdraw(s, hist, amount, reason="Withdrawal"):
    amount = float(amount)
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Withdrawal must be a positive amount.")
    if amount > cash_now(s) + 1e-9:
        raise ValueError(f"Only ${cash_now(s):,.2f} in cash. Sell something first.")
    d = last_close(hist, BENCH)[1]
    ev = make_event(d, "WITHDRAW", "CASH", 0, 1.0, -amount, reason or "Withdrawal")
    s["events"].append(ev)
    return ev


# ---------- fees and fills ----------
def fee_breakdown(s, d, side, shares, px):
    """Commission on every trade; SEC Section 31 fee and FINRA TAF on sells only.
    Both regulatory fees are rounded up to the cent, with a one-cent minimum."""
    shares = abs(shares)
    commission = get_setting(s, "fee", d, 0.0) + get_setting(s, "fee_per_share", d, 0.0) * shares
    sec = taf = 0.0
    if side == "SELL":
        rate = get_setting(s, "sec_fee_per_million", d, 0.0)
        if rate > 0:
            sec = max(0.01, math.ceil(shares * px * rate / 1e6 * 100 - 1e-9) / 100)
        per_sh = get_setting(s, "taf_per_share", d, 0.0)
        if per_sh > 0:
            raw = min(per_sh * shares, get_setting(s, "taf_max", d, 9.79))
            taf = max(0.01, math.ceil(raw * 100 - 1e-9) / 100)
    return {"commission": commission, "sec": sec, "taf": taf, "total": commission + sec + taf}


def _fill(s, t, side, d, px_mkt, dollars, shares, reason, spread=True, order_id=None):
    """Create the trade event. spread=False is used for limit orders, which fill at their price."""
    frac = get_setting(s, "fractional", d, True)
    rate = get_setting(s, "tax_rate", d, 0.0)
    half = 0.0
    if spread:
        half = (get_setting(s, "spread_pct", d, 0.0) / 2 + get_setting(s, "slippage_pct", d, 0.0)) / 100
    if side == "BUY":
        px = px_mkt * (1 + half)
        sh = shares if shares else dollars / px
        sh = sh if frac else math.floor(sh + 1e-12)
        if sh <= 0:
            raise ValueError("Amount too small to buy one share.")
        fb = fee_breakdown(s, d, side, sh, px)
        cash = -(sh * px + fb["total"])
        if cash_now(s) + cash < -0.005:
            raise ValueError(f"Not enough cash for this buy (needs ${-cash:,.2f}, have ${cash_now(s):,.2f}).")
        ev = make_event(d, "BUY", t, sh, px, cash, reason, fb["total"], sh * (px - px_mkt) + fb["total"], rate)
    else:
        held = position(s, t, "9999-12-31")
        px = px_mkt * (1 - half)
        sh = shares if shares else dollars / px
        sh = min(sh, held)
        sh = sh if frac else math.floor(sh + 1e-12)
        if sh <= 0:
            raise ValueError(f"Nothing to sell of {t}.")
        fb = fee_breakdown(s, d, side, sh, px)
        ev = make_event(d, "SELL", t, -sh, px, sh * px - fb["total"], reason, fb["total"],
                        sh * (px_mkt - px) + fb["total"], rate)
    ev["fee_detail"] = {k: round(v, 4) for k, v in fb.items()}
    if order_id:
        ev["order"] = order_id
    s["events"].append(ev)
    return ev


def place_trade(s, hist, t, side, dollars, reason, shares=None):
    """Immediate market fill at the latest close. Used by rebalance."""
    if t not in hist:
        raise RuntimeError(f"Refresh first: no price data for {t}.")
    px_mkt, d = last_close(hist, t)
    return _fill(s, t, side, d, px_mkt, dollars, shares, reason)


# ---------- orders ----------
def place_order(s, hist, t, side, kind, reason, dollars=None, shares=None,
                limit=None, tif="GTC", expires=None):
    """MARKET fills now at the latest close. LIMIT waits and fills on a later daily bar."""
    side, kind, tif = side.upper(), kind.upper(), tif.upper()
    if side not in ("BUY", "SELL") or kind not in ("MARKET", "LIMIT") or tif not in ("DAY", "GTC"):
        raise ValueError("Side must be BUY or SELL, type MARKET or LIMIT, time in force DAY or GTC.")
    dollars, shares = (dollars or None), (shares or None)
    if bool(dollars) == bool(shares):
        raise ValueError("Give either a dollar amount or a share count, not both and not neither.")
    if (dollars or shares) <= 0:
        raise ValueError("Amount must be positive.")
    if kind == "LIMIT" and not (limit and limit > 0):
        raise ValueError("A limit order needs a limit price above zero.")
    if t not in hist:
        raise RuntimeError(f"Refresh first: no price data for {t}.")
    px_mkt, d = last_close(hist, t)
    o = {"id": f"O{len(s['orders']) + 1}", "placed": d,
         "placed_at": dt.datetime.now().isoformat(timespec="seconds"),
         "ticker": t, "side": side, "type": kind, "dollars": dollars, "shares": shares,
         "limit": float(limit) if kind == "LIMIT" else None, "tif": tif,
         "expires": expires or None, "reason": reason, "status": "OPEN",
         "fill_date": None, "fill_price": None, "note": ""}
    if kind == "MARKET":
        ev = _fill(s, t, side, d, px_mkt, dollars, shares, reason, order_id=o["id"])   # raises if rejected
        o.update(status="FILLED", fill_date=d, fill_price=ev["price"])
    s["orders"].append(o)
    return o


def cancel_order(s, oid):
    for o in s["orders"]:
        if o["id"] == oid:
            if o["status"] != "OPEN":
                raise ValueError(f"{oid} is already {o['status']}.")
            o["status"], o["note"] = "CANCELED", f"Canceled {today()}"
            return o
    raise ValueError(f"No order {oid}.")


def process_orders(s, hist):
    """Fill, expire or leave open each open limit order against bars after it was placed.
    Candidate fills are applied oldest first so cash checks happen in date order."""
    cands = []
    for o in s["orders"]:
        if o["status"] != "OPEN" or o["ticker"] not in hist:
            continue
        h = hist[o["ticker"]]
        bars = h[h.index > pd.Timestamp(o["placed"])].dropna(subset=["Close"])
        if o["tif"] == "DAY":
            bars = bars.iloc[:1]
        hit = None
        for ts, bar in bars.iterrows():
            ds = ts.date().isoformat()
            if o["expires"] and ds > o["expires"]:
                break
            if o["side"] == "BUY" and bar["Low"] < o["limit"]:
                hit = (ds, min(o["limit"], float(bar["Open"])))
            elif o["side"] == "SELL" and bar["High"] > o["limit"]:
                hit = (ds, max(o["limit"], float(bar["Open"])))
            if hit:
                break
        if hit:
            cands.append((hit[0], o["id"], o, hit[1]))
        elif o["tif"] == "DAY" and len(bars):
            o["status"], o["note"] = "EXPIRED", "Day order did not trade through its limit"
        elif o["expires"] and today() > o["expires"]:
            o["status"], o["note"] = "EXPIRED", f"Expired {o['expires']}"
    for ds, _, o, px in sorted(cands, key=lambda c: (c[0], c[1])):
        try:
            ev = _fill(s, o["ticker"], o["side"], ds, px, o["dollars"], o["shares"], o["reason"],
                       spread=False, order_id=o["id"])
            o.update(status="FILLED", fill_date=ds, fill_price=ev["price"])
        except ValueError as e:
            o.update(status="REJECTED", note=str(e))


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
def flow_series(s, cal):
    """Money put in (+) or taken out (-) on each calendar day: opening cash and positions, deposits, withdrawals."""
    f = pd.Series(0.0, index=cal)
    for e in s["events"]:
        if e["kind"] in FLOW_KINDS:
            amt = e["cash"]
        elif e["kind"] == "OPEN":
            amt = e["shares"] * e["price"]
        else:
            continue
        pos = min(cal.searchsorted(pd.Timestamp(e["date"])), len(cal) - 1)
        f.iloc[pos] += amt
    return f


def twr_index(values, flows):
    """Time-weighted growth of 1. A flow lands at the close, so it earns nothing that day."""
    out, prev = [1.0], None
    for i in range(1, len(values)):
        prev = values.iloc[i - 1]
        r = (values.iloc[i] - flows.iloc[i]) / prev - 1 if prev > 0 else 0.0
        out.append(out[-1] * (1 + r))
    return pd.Series(out, index=values.index)


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


def benchmark(hist, closes, flows):
    """Same dollars, same dates, all in SPY with dividends reinvested, no costs."""
    c = closes[BENCH]
    d = hist[BENCH]["Dividends"].reindex(closes.index).fillna(0.0)
    sh, vals = 0.0, []
    for day in closes.index:
        if d[day] > 0:
            sh += sh * d[day] / c[day]
        sh += flows[day] / c[day]
        vals.append(sh * c[day])
    v = pd.Series(vals, index=closes.index)
    return pd.DataFrame({"value": v, "twr": twr_index(v, flows)})


def refresh(s):
    migrate(s)
    tickers = sorted(set(s["tickers"]) | {BENCH})
    hist = fetch(tickers, s["start_date"])
    if not s["opened"]:
        open_portfolio(s, hist)
    check_splits(hist, s["start_date"])
    idx = hist[BENCH].index
    cal = idx[idx >= pd.Timestamp(s["start_date"])]
    process_orders(s, hist)
    process_dividends(s, hist, cal)
    closes = build_closes(hist, cal)
    flows = flow_series(s, cal)
    r = replay(s, closes)
    r["flow"] = flows
    r["twr"] = twr_index(r["net"], flows)
    return hist, r, benchmark(hist, closes, flows)


# ---------- reporting ----------
def metrics(s, r, bench):
    contributed = float(r["flow"].sum())
    net = r["net"]
    trades = [e for e in s["events"] if e["kind"] in ("BUY", "SELL")]
    traded = sum(abs(e["shares"] * e["price"]) for e in trades)
    sb = r["twr"].iloc[-1] - 1
    bb = bench["twr"].iloc[-1] - 1
    reg = sum(e.get("fee_detail", {}).get("sec", 0) + e.get("fee_detail", {}).get("taf", 0) for e in trades)
    return [
        ("Money put in (net of withdrawals)", f"${contributed:,.2f}"),
        ("Sandbox net value", f"${net.iloc[-1]:,.2f}"),
        ("Net profit", f"${net.iloc[-1] - contributed:,.2f}"),
        ("Benchmark value (same cash flows in SPY)", f"${bench['value'].iloc[-1]:,.2f}"),
        ("Cash on hand", f"${r['cash'].iloc[-1]:,.2f}"),
        ("Sandbox return (time-weighted, net)", f"{sb * 100:.2f}%"),
        ("Benchmark return (SPY total return)", f"{bb * 100:.2f}%"),
        ("Excess return vs benchmark", f"{(sb - bb) * 100:.2f} pts"),
        ("Max drawdown (sandbox)", f"{(r['twr'] / r['twr'].cummax() - 1).min() * 100:.2f}%"),
        ("Estimated tax liability", f"${r['liability'].iloc[-1]:,.2f}"),
        ("Trades / dollars traded", f"{len(trades)} / ${traded:,.2f}"),
        ("Turnover (dollars traded / avg value)", f"{traded / net.mean():.2f}x" if net.mean() > 0 else "n/a"),
        ("Total costs logged", f"${sum(e['cost'] for e in s['events']):,.2f}"),
        ("of which SEC + FINRA fees", f"${reg:,.2f}"),
    ]


def segment_rows(s, r, bench):
    first, last = r.index[0], r.index[-1]
    cuts = sorted({x["effective"] for x in s["settings"] if s["start_date"] < x["effective"] <= last.date().isoformat()})
    pts = [first] + [pd.Timestamp(c) for c in cuts] + [last]
    rows = []
    for a, b in zip(pts[:-1], pts[1:]):
        sr = r["twr"].asof(b) / r["twr"].asof(a) - 1
        br = bench["twr"].asof(b) / bench["twr"].asof(a) - 1
        rows.append((a.date(), b.date(), f"{sr * 100:.2f}%", f"{br * 100:.2f}%", f"{(sr - br) * 100:.2f} pts"))
    return rows


def draw_chart(s, r, bench):
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    ax[0].plot(r.index, r["net"], color="#4fc3f7", label="Sandbox (net)")
    ax[0].plot(bench.index, bench["value"], color="#ffb74d", label="SPY, same cash flows")
    ax[0].set_title("Value ($)")
    ax[1].plot(r.index, (r["twr"] - 1) * 100, color="#4fc3f7", label="Sandbox")
    ax[1].plot(bench.index, (bench["twr"] - 1) * 100, color="#ffb74d", label="SPY")
    ax[1].set_title("Time-weighted return since start (%)")
    last = r.index[-1].date().isoformat()
    for c in {x["effective"] for x in s["settings"] if s["start_date"] < x["effective"] <= last}:
        for a in ax:
            a.axvline(pd.Timestamp(c), color="#777", ls="--", lw=0.8)
    for a in ax:
        a.grid(True, alpha=0.25)
        a.spines[["top", "right"]].set_visible(False)
        a.legend(fontsize=9, frameon=False)
    fig.tight_layout()
    return fig


def holdings_rows(s, hist):
    total_mv, rows = 0.0, []
    for t in sorted(set(s["tickers"])):
        sh = position(s, t, "9999-12-31")
        if sh <= 1e-9:
            continue
        px, _ = last_close(hist, t)
        mv = sh * px
        total_mv += mv
        rows.append([t, sh, px, mv])
    cash = cash_now(s)
    total = total_mv + cash
    out = []
    for t, sh, px, mv in rows:
        out.append([t, f"{sh:,.4f}", f"{px:,.2f}", f"${mv:,.2f}",
                    f"{mv / total * 100:.1f}%" if total > 0 else "",
                    f"{get_setting(s, f'reinvest_{t}', today(), 0.0):.0f}%",
                    f"{s['targets'].get(t, 0.0) * 100:.1f}%"])
    out.append(["CASH", "", "", f"${cash:,.2f}", f"{cash / total * 100:.1f}%" if total > 0 else "", "", ""])
    return out
