"""Performance, risk and attribution metrics from a per-bar backtest ledger."""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 365.0


# ---------------------------------------------------------------------------
def _daily_sum(s: pd.Series) -> pd.Series:
    return s.resample("1D").sum()


def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp, pd.Timestamp, float]:
    """(max drawdown, peak date, trough date, longest underwater stretch in days).

    O(n), pure numpy -- the naive `.idxmax()`-in-a-loop version is O(n^2) and becomes
    unusable on a 15m/5-year panel.
    """
    v = equity.to_numpy(dtype="float64")
    idx = equity.index
    n = len(v)
    if n == 0:
        return 0.0, pd.NaT, pd.NaT, 0.0

    peak = np.maximum.accumulate(v)
    dd = np.where(peak > 0, v / peak - 1.0, 0.0)
    k = int(np.argmin(dd))
    mdd = float(dd[k])
    start = idx[int(np.argmax(v[: k + 1]))] if k >= 0 else idx[0]
    end = idx[k]

    # longest peak -> recovery stretch (unfinished stretches run to the last bar)
    longest = 0.0
    peak_i = 0
    cur_start = None
    for i in range(n):
        if v[i] >= v[peak_i]:
            if cur_start is not None:
                longest = max(longest, (idx[i] - idx[cur_start]).total_seconds() / 86400.0)
                cur_start = None
            peak_i = i
        elif cur_start is None:
            cur_start = peak_i
    if cur_start is not None:
        longest = max(longest, (idx[-1] - idx[cur_start]).total_seconds() / 86400.0)
    return mdd, start, end, float(longest)


def compute_metrics(bars: pd.DataFrame, name: str = "strategy") -> Dict[str, float]:
    eq = bars["equity"]
    years = max((eq.index[-1] - eq.index[0]).total_seconds() / 86400.0 / 365.25, 1e-6)
    daily = _daily_sum(bars["net_ret"])
    mdd, dd_start, dd_end, dd_dur = max_drawdown(eq)

    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if eq.iloc[-1] > 0 else -1.0
    ann_ret = float(daily.mean() * TRADING_DAYS)
    ann_vol = float(daily.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(daily) > 2 else np.nan
    sharpe = ann_ret / ann_vol if ann_vol and np.isfinite(ann_vol) and ann_vol > 0 else np.nan

    downside = daily[daily < 0]
    dvol = float(downside.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(downside) > 2 else np.nan
    sortino = ann_ret / dvol if dvol and np.isfinite(dvol) and dvol > 0 else np.nan
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan

    pos, neg = daily[daily > 0], daily[daily < 0]
    win = float((daily > 0).mean())
    pf = float(pos.sum() / abs(neg.sum())) if len(neg) and neg.sum() != 0 else np.nan

    # per-holding-period (rebalance-to-rebalance) trade statistics
    per_bar = bars["net_ret"]
    long_r, short_r = bars["long_ret"], bars["short_ret"]

    total_turnover = float(bars["turnover"].sum())
    gx = bars["gross_exposure"]
    abs_gx = float(gx.mean())
    ann_turnover = total_turnover / years

    # Cost accounting.  `funding` is a cashflow with POSITIVE = income, whereas the
    # fee/spread/impact columns are costs.  Keep the two separate in the output so a
    # funding *credit* is never presented as an expense.
    trading_cost = float(bars[["fee", "spread", "impact"]].sum().sum())
    funding_pnl = float(bars["funding"].sum())

    res = {
        "name": name,
        "start": str(eq.index[0].date()), "end": str(eq.index[-1].date()), "years": years,
        "CAGR": cagr,
        "Annualized Return (daily-mean)": ann_ret,
        "Annualized Volatility": ann_vol,
        "Sharpe": sharpe,
        "Sortino": sortino,
        "Calmar": calmar,
        "Max Drawdown": mdd,
        "Max DD Duration (days)": dd_dur,
        "Max DD Start": str(dd_start.date()) if pd.notna(dd_start) else "",
        "Max DD End": str(dd_end.date()) if pd.notna(dd_end) else "",
        "Win Rate (daily)": win,
        "Profit Factor (daily)": pf,
        "Average Trade (per bar)": float(per_bar.mean()),
        "Median Trade (per bar)": float(per_bar.median()),
        "Annualized Return / bar": float(per_bar.mean() * bars.shape[0] / years),
        "Skew": float(daily.skew()), "Kurtosis": float(daily.kurtosis()),
        "VaR 95 (daily)": float(daily.quantile(0.05)),
        "CVaR 95 (daily)": float(daily[daily <= daily.quantile(0.05)].mean()) if len(daily) else np.nan,

        "Long PnL (total)": float(long_r.sum()),
        "Short PnL (total)": float(short_r.sum()),
        "Gross PnL (total)": float(bars["gross_ret"].sum()),
        "Net PnL (total)": float(bars["net_ret"].sum()),
        "Long Share of Gross": float(long_r.sum() / bars["gross_ret"].sum())
        if bars["gross_ret"].sum() != 0 else np.nan,
        "Long Win Rate (bar)": float((long_r > 0).mean()),
        "Short Win Rate (bar)": float((short_r > 0).mean()),

        "Gross Exposure (avg)": abs_gx,
        "Net Exposure (avg)": float(bars["net_exposure"].mean()),
        "Net Exposure (avg abs)": float(bars["net_exposure"].abs().mean()),
        "Beta Exposure (avg)": float(bars["beta_exposure"].mean()),
        "Beta Exposure (avg abs)": float(bars["beta_exposure"].abs().mean()),
        "Beta Exposure (std)": float(bars["beta_exposure"].std()),

        "Average Turnover (per bar)": float(bars["turnover"].mean()),
        "Average Daily Turnover": total_turnover / max(years * TRADING_DAYS, 1e-9),
        "Annual Turnover": ann_turnover,
        "Turnover (total, x gross)": total_turnover,

        "Trading Fee (total, frac)": float(bars["fee"].sum()),
        "Spread Cost (total, frac)": float(bars["spread"].sum()),
        "Slippage/Impact (total, frac)": float(bars["impact"].sum()),
        # bars["funding"] is the funding *cashflow* (-sum w*rate): POSITIVE = income.
        # A dollar-neutral book mostly nets funding out, so this is usually small and
        # can legitimately be positive.  Reporting it as a "cost" and adding it into
        # the cost drag would double-count income as an expense.
        "Funding P&L (total, frac)": float(funding_pnl),
        "Trading Cost (total, frac)": float(trading_cost),
        "Total Cost (frac)": float(trading_cost - funding_pnl),
        "Trading Cost Drag (annual)": float(trading_cost / years),
        "Cost Drag (annual)": float((trading_cost - funding_pnl) / years),
        "Return / Turnover": (cagr / ann_turnover) if ann_turnover > 1e-9 else np.nan,
        "Avg Max Participation": float(bars["max_participation"].mean()),
    }
    res["_daily_returns"] = daily
    res["_equity"] = eq
    return res


def fmt_metrics(m: Dict, keys: Optional[list] = None) -> pd.DataFrame:
    pct = {"CAGR", "Annualized Return (daily-mean)", "Annualized Volatility", "Max Drawdown",
           "Win Rate (daily)", "Average Trade (per bar)", "Median Trade (per bar)",
           "VaR 95 (daily)", "CVaR 95 (daily)", "Long Share of Gross",
           "Long Win Rate (bar)", "Short Win Rate (bar)", "Annualized Return / bar",
           "Cost Drag (annual)", "Trading Cost Drag (annual)", "Average Daily Turnover"}
    rows = []
    for k, v in m.items():
        if k.startswith("_"):
            continue
        if keys and k not in keys:
            continue
        if isinstance(v, str):
            rows.append((k, v))
        elif isinstance(v, (int, float, np.floating)) and np.isfinite(v):
            rows.append((k, f"{v:.4%}" if k in pct else f"{v:,.4f}"))
        else:
            rows.append((k, "n/a"))
    return pd.DataFrame(rows, columns=["metric", "value"])


def monthly_returns(bars: pd.DataFrame) -> pd.DataFrame:
    eq = bars["equity"]
    m = eq.resample("1ME").last()
    m0 = eq.resample("1ME").first()
    prev = eq.resample("1ME").last().shift(1)
    prev.iloc[0] = eq.iloc[0]
    r = (m / prev - 1.0)
    out = pd.DataFrame({"ret": r})
    out["year"] = out.index.year
    out["month"] = out.index.month
    return out.pivot_table(index="year", columns="month", values="ret")


def rolling_stats(bars: pd.DataFrame, window_days: int = 30) -> pd.DataFrame:
    daily = _daily_sum(bars["net_ret"])
    w = window_days
    mean = daily.rolling(w).mean() * TRADING_DAYS
    vol = daily.rolling(w).std(ddof=1) * np.sqrt(TRADING_DAYS)
    out = pd.DataFrame({
        "roll_ann_ret": mean,
        "roll_ann_vol": vol,
        "roll_sharpe": mean / vol.replace(0, np.nan),
        "roll_beta": bars["beta_exposure"].resample("1D").last().rolling(w).mean(),
    })
    return out


def per_period_returns(bars: pd.DataFrame, reb_ts: pd.DatetimeIndex) -> pd.Series:
    """Compound net return between consecutive rebalance timestamps (a 'trade')."""
    idx = pd.Series(np.arange(len(bars)), index=bars.index)
    bounds = []
    for t in reb_ts:
        loc = idx.index.searchsorted(t)
        if loc < len(bars):
            bounds.append(loc)
    bounds = sorted(set(bounds + [len(bars)]))
    vals = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = bars["net_ret"].iloc[a:b]
        vals.append(float((1 + seg).prod() - 1))
    return pd.Series(vals)


# ---------------------------------------------------------------------------
# cost scenarios -- the single most important decomposition in this research
# ---------------------------------------------------------------------------
def bars_variant(bars: pd.DataFrame, kind: str) -> pd.DataFrame:
    """Rebuild a ledger with a different cost assumption.

    gross            : price P&L on the same positions, no fees / funding at all
    no_trading_cost  : gross + funding          (a maker-only / zero-fee venue)
    no_funding       : gross - trading costs    (funding = 0)
    net              : the real thing

    ``bars["funding"]`` is already the signed cashflow (positive = income), so it is
    added, never subtracted.
    """
    out = bars.copy()
    trading = bars["fee"] + bars["spread"] + bars["impact"]
    if kind == "gross":
        new = bars["gross_ret"]
    elif kind == "no_trading_cost":
        new = bars["gross_ret"] + bars["funding"]
    elif kind == "no_funding":
        new = bars["gross_ret"] - trading
    elif kind == "net":
        new = bars["net_ret"]
    else:
        raise ValueError(kind)
    out["net_ret"] = new
    out["equity"] = (1 + new).cumprod()
    out["drawdown"] = out["equity"] / out["equity"].cummax() - 1.0
    return out


COST_SCENARIOS = ("gross", "no_trading_cost", "no_funding", "net")


def cost_scenario_table(bars: pd.DataFrame) -> pd.DataFrame:
    """CAGR / Sharpe / maxDD under each cost assumption, plus the annual drag."""
    rows = []
    base = compute_metrics(bars, "net")
    for kind in COST_SCENARIOS:
        m = compute_metrics(bars_variant(bars, kind), kind) if kind != "net" else base
        rows.append({
            "scenario": kind,
            "CAGR": m["CAGR"],
            "ann_vol": m["Annualized Volatility"],
            "Sharpe": m["Sharpe"],
            "Sortino": m["Sortino"],
            "max_dd": m["Max Drawdown"],
            "ann_turnover": m["Annual Turnover"],
            "CAGR_gap_vs_gross": m["CAGR"] - compute_metrics(bars_variant(bars, "gross"),
                                                             "g")["CAGR"],
        })
    return pd.DataFrame(rows)
