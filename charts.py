"""Plotly chart builders for the app. Dark, flat, no gridlines, green when up and orange-red when down."""
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

UP, DOWN, MUTED, FAINT = "#00C805", "#FF5000", "#8b9099", "rgba(255,255,255,0.07)"
RANGES = ["1W", "1M", "3M", "6M", "YTD", "1Y", "5Y", "ALL"]


def cut_start(index, rng):
    """First timestamp to keep for a range label, measured back from the last date in the index."""
    last = index[-1]
    days = {"1W": 7, "1M": 30, "3M": 91, "6M": 182, "1Y": 365, "5Y": 1826}
    if rng == "YTD":
        return pd.Timestamp(year=last.year, month=1, day=1)
    if rng in days:
        return last - pd.Timedelta(days=days[rng])
    return index[0]


def slice_range(obj, rng):
    return obj[obj.index >= cut_start(obj.index, rng)]


def _base(fig, height):
    fig.update_layout(
        height=height, margin=dict(l=0, r=0, t=6, b=0), showlegend=False, hovermode="x unified",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#c9cdd2", size=12),
        hoverlabel=dict(bgcolor="#1b1e24", bordercolor="#2a2e36", font=dict(color="#f2f3f5")),
        dragmode="pan")
    fig.update_xaxes(showgrid=False, zeroline=False, showline=False, tickfont=dict(color=MUTED),
                     spikemode="across", spikethickness=1, spikecolor="#4a4f58", spikedash="solid")
    return fig


def portfolio_fig(net, bench_value, twr, rng, show_bench):
    """Portfolio value over a range. Color follows the time-weighted return over that range."""
    net = slice_range(net, rng)
    start = net.index[0]
    color = UP
    t = twr[twr.index >= start]
    if len(t) > 1 and t.iloc[-1] < t.iloc[0]:
        color = DOWN
    fig = go.Figure()
    if show_bench:
        b = bench_value[bench_value.index >= start]
        fig.add_trace(go.Scatter(x=b.index, y=b, name="SPY, same cash flows", mode="lines",
                                 line=dict(color=MUTED, width=1.5, dash="dot"), hovertemplate="%{y:$,.2f}"))
    fig.add_trace(go.Scatter(x=net.index, y=net, name="Your portfolio", mode="lines+markers" if len(net) < 4 else "lines",
                             line=dict(color=color, width=2.2), marker=dict(size=8, color=color),
                             hovertemplate="%{y:$,.2f}"))
    _base(fig, 320)
    fig.update_yaxes(showgrid=True, gridcolor=FAINT, zeroline=False, showline=False, side="right",
                     tickprefix="$", tickformat=",.0f", tickfont=dict(color=MUTED))
    lo, hi = float(net.min()), float(net.max())
    pad = max((hi - lo) * 0.15, hi * 0.002)
    fig.update_yaxes(range=[lo - pad, hi + pad])
    return fig


def stock_fig(df, trades, rng, kind="Line", ma=False):
    """Price chart with volume underneath. df has Open High Low Close Volume. trades is a list of
    (date, side, price) to mark with triangles."""
    full = df
    d = slice_range(df, rng)
    up = len(d) < 2 or d["Close"].iloc[-1] >= d["Close"].iloc[0]
    color = UP if up else DOWN
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.8, 0.2], vertical_spacing=0.02)
    if kind == "Candles":
        fig.add_trace(go.Candlestick(x=d.index, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"],
                                     increasing_line_color=UP, decreasing_line_color=DOWN,
                                     increasing_fillcolor=UP, decreasing_fillcolor=DOWN, name="Price"), row=1, col=1)
    else:
        fig.add_trace(go.Scatter(x=d.index, y=d["Close"], mode="lines", name="Close",
                                 line=dict(color=color, width=2), hovertemplate="%{y:$,.2f}"), row=1, col=1)
    if ma and len(full) >= 50:
        m = full["Close"].rolling(50).mean()
        m = m[m.index >= d.index[0]].dropna()
        fig.add_trace(go.Scatter(x=m.index, y=m, mode="lines", name="50-day average",
                                 line=dict(color="#7aa2ff", width=1.4, dash="dot"), hovertemplate="%{y:$,.2f}"), row=1, col=1)
    for side, symbol, c in (("BUY", "triangle-up", UP), ("SELL", "triangle-down", DOWN)):
        pts = [(x, p) for x, s_, p in trades if s_ == side and pd.Timestamp(x) >= d.index[0]]
        if pts:
            fig.add_trace(go.Scatter(x=[pd.Timestamp(x) for x, _ in pts], y=[p for _, p in pts], mode="markers", name=side.title(),
                                     marker=dict(symbol=symbol, size=12, color=c, line=dict(color="#0b0c0f", width=1)),
                                     hovertemplate=side.title() + " at %{y:$,.2f}"), row=1, col=1)
    vol_colors = [UP if c >= o else DOWN for o, c in zip(d["Open"], d["Close"])]
    fig.add_trace(go.Bar(x=d.index, y=d["Volume"], marker_color=vol_colors, marker_opacity=0.45, name="Volume",
                         hovertemplate="%{y:,.0f}"), row=2, col=1)
    _base(fig, 430)
    fig.update_layout(xaxis_rangeslider_visible=False)
    fig.update_yaxes(showgrid=True, gridcolor=FAINT, zeroline=False, side="right", tickprefix="$",
                     tickfont=dict(color=MUTED), row=1, col=1)
    fig.update_yaxes(showgrid=False, showticklabels=False, row=2, col=1)
    return fig


def dividend_fig(div):
    """Bar chart of the last payments. div is a Series of per-share dividends indexed by date."""
    d = div.tail(12)
    fig = go.Figure(go.Bar(x=d.index, y=d, marker_color=UP, marker_opacity=0.85, hovertemplate="%{y:$,.4f} per share"))
    _base(fig, 190)
    fig.update_yaxes(showgrid=True, gridcolor=FAINT, tickprefix="$", tickfont=dict(color=MUTED), side="right")
    return fig


def allocation_fig(labels, values):
    palette = ["#00C805", "#4fc3f7", "#b388ff", "#ffb74d", "#ff6e91", "#7aa2ff", "#5dd6b2", "#c9cdd2"]
    fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.68, sort=False, textinfo="none",
                           marker=dict(colors=[palette[i % len(palette)] for i in range(len(labels))],
                                       line=dict(color="#0b0c0f", width=2)),
                           hovertemplate="%{label}: %{value:$,.2f} (%{percent})<extra></extra>"))
    fig.update_layout(height=250, margin=dict(l=0, r=0, t=0, b=0), showlegend=True,
                      legend=dict(font=dict(color="#c9cdd2"), orientation="v", x=1.0),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return fig
