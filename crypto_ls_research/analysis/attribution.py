"""Attribution: long vs short leg, per-factor ablation, market-regime slicing, per-coin P&L."""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from ..backtest.engine import BacktestResult
from ..data.store import Panels
from ..factors.engine import FACTOR_NAMES
from .metrics import compute_metrics

TRADING_DAYS = 365.0


# ---------------------------------------------------------------------------
def leg_bars(bars: pd.DataFrame, leg: str) -> pd.DataFrame:
    col = "long_ret" if leg == "long" else "short_ret"
    out = bars.copy()
    out["net_ret"] = bars[col]
    out["equity"] = (1 + out["net_ret"]).cumprod()
    out["drawdown"] = out["equity"] / out["equity"].cummax() - 1.0
    for c in ("fee", "spread", "impact", "funding", "turnover"):
        out[c] = 0.0
    return out


def long_short_attribution(bars: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    for leg, col in (("LONG", "long_ret"), ("SHORT", "short_ret"), ("NET", "net_ret")):
        m = compute_metrics(leg_bars(bars, "long" if leg == "LONG" else "short")
                            if leg != "NET" else bars, name=leg)
        rows[leg] = m
    table = []
    for leg, m in rows.items():
        table.append({
            "leg": leg,
            "total_return": m["Net PnL (total)"],
            "CAGR": m["CAGR"],
            "ann_vol": m["Annualized Volatility"],
            "Sharpe": m["Sharpe"],
            "Sortino": m["Sortino"],
            "max_dd": m["Max Drawdown"],
            "calmar": m["Calmar"],
            "win_rate_daily": m["Win Rate (daily)"],
            "skew": m["Skew"],
        })
    return pd.DataFrame(table)


# ---------------------------------------------------------------------------
def factor_ablation(panels: Panels, cfg, run_fn,
                    subsets: Sequence[Sequence[str]] | None = None,
                    label_fn=None) -> Dict[str, BacktestResult]:
    """Run the engine once per factor subset.  `run_fn(subset, profile) -> BacktestResult`.

    Marginal information is judged by comparing:
        single-factor runs, all pairwise runs, the full 4-factor run,
        and leave-one-out runs (drop one factor at a time).
    """
    from itertools import combinations
    F = tuple(FACTOR_NAMES)
    if subsets is None:
        subsets = []
        subsets += [(k,) for k in F]
        subsets += [tuple(c) for c in combinations(F, 2)]
        subsets += [tuple(sorted(set(F) - {k})) for k in F]
        subsets += [F]
    seen, uniq = set(), []
    for s in subsets:
        key = tuple(sorted(s))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(tuple(s))
    out: Dict[str, BacktestResult] = {}
    for s in uniq:
        prof = _profile_for(cfg, s)
        name = label_fn(s) if label_fn else "+".join(s)
        out[name] = run_fn(s, prof)
    return out


def _profile_for(cfg, subset) -> np.ndarray:
    base = np.asarray(cfg.factors.profiles[cfg.factors.default_profile], dtype="float64")
    idx = [FACTOR_NAMES.index(k) for k in subset]
    return base[idx]


def factor_ablation_table(runs: Dict[str, BacktestResult], key: str = "full") -> pd.DataFrame:
    rows = []
    full = runs[key]
    base_ret = full.bars["net_ret"]
    for name, res in runs.items():
        m = compute_metrics(res.bars, name=name)
        corr = float(np.corrcoef(res.bars["net_ret"], base_ret)[0, 1]) if name != key else 1.0
        rows.append({
            "run": name, "n_factors": len(res.meta["factor_subset"]),
            "CAGR": m["CAGR"], "ann_vol": m["Annualized Volatility"], "Sharpe": m["Sharpe"],
            "max_dd": m["Max Drawdown"], "Sortino": m["Sortino"],
            "avg_gross": m["Gross Exposure (avg)"],
            "ann_turnover": m["Annual Turnover"],
            "ann_cost_drag": m["Cost Drag (annual)"],
            "corr_to_full": corr,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# market-regime slicing
# ---------------------------------------------------------------------------
def regime_labels(panels: Panels, cfg) -> pd.DataFrame:
    """Per-bar regime tags, every one of them built from trailing data only."""
    bpd = cfg.bars_per_day
    btc = panels.close[cfg.benchmark_inst]

    def mom(days: float) -> pd.Series:
        return btc / btc.shift(max(2, int(round(days * bpd)))) - 1.0

    btc30, btc60 = mom(30), mom(60)
    lr = np.log(btc.where(btc > 0)).diff()
    bvol = lr.rolling(max(5, int(round(10 * bpd))), min_periods=5).std(ddof=0) * np.sqrt(cfg.bars_per_year)

    # alt breadth: median 30d return across the trailing PIT pool members
    ret30 = panels.close / panels.close.shift(max(2, int(round(30 * bpd)))) - 1.0
    adv = panels.amount.rolling(max(5, int(round(30 * bpd))), min_periods=5).mean()
    elig = adv >= cfg.universe.min_avg_amount_usd
    alt30 = ret30.where(elig).median(axis=1)

    out = pd.DataFrame(index=panels.index)
    out["btc_mom_30d"] = btc30
    out["btc_mom_60d"] = btc60
    out["btc_vol_ann"] = bvol
    out["alt_minus_btc_30d"] = alt30 - btc30

    out["trend"] = np.select(
        [btc30 > 0.05, btc30 < -0.05], ["Bull", "Bear"], default="Sideways")
    out.loc[btc30.isna(), "trend"] = "n/a"
    out["vol_regime"] = np.select(
        [bvol < 0.40, bvol > 0.80], ["LowVol", "HighVol"], default="MidVol")
    out.loc[bvol.isna(), "vol_regime"] = "n/a"
    out["btc_trend_strength"] = np.select(
        [btc60 > 0.15, btc60 < -0.15], ["BTC_Strong", "BTC_Weak"], default="BTC_Flat")
    out.loc[btc60.isna(), "btc_trend_strength"] = "n/a"
    out["alt_season"] = np.select(
        [out["alt_minus_btc_30d"] > 0.03, out["alt_minus_btc_30d"] < -0.03],
        ["AltcoinSeason", "BTC_Dominance_Rising"], default="Balanced")
    out.loc[alt30.isna(), ["alt_season"]] = "n/a"
    return out


def regime_attribution(bars: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    joined = bars.join(labels, how="left")
    rows = []
    for dim in ("trend", "vol_regime", "btc_trend_strength", "alt_season"):
        for grp, seg in joined.groupby(dim, observed=True):
            if len(seg) < 10 or grp == "n/a":
                continue
            d = seg["net_ret"].resample("1D").sum()
            ann_ret = float(d.mean() * TRADING_DAYS)
            ann_vol = float(d.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(d) > 2 else np.nan
            eq = (1 + seg["net_ret"]).cumprod()
            dd = float((eq / eq.cummax() - 1).min())
            rows.append({
                "dimension": dim, "regime": grp, "bars": len(seg),
                "days": len(d),
                "share_of_time": len(d) / max(len(joined["net_ret"].resample("1D").sum()), 1),
                "total_ret": float(eq.iloc[-1] - 1),
                "ann_ret": ann_ret, "ann_vol": ann_vol,
                "sharpe": ann_ret / ann_vol if ann_vol and ann_vol > 0 else np.nan,
                "max_dd": dd,
                "contrib_to_total": float(seg["net_ret"].sum()),
                "avg_gross": float(seg["gross_exposure"].mean()),
            })
    return pd.DataFrame(rows).sort_values(["dimension", "contrib_to_total"],
                                          ascending=[True, False])


# ---------------------------------------------------------------------------
# per-coin attribution
# ---------------------------------------------------------------------------
def coin_attribution(result: BacktestResult, top_n: int = 25) -> pd.DataFrame:
    g = result.name_gross.sum(axis=0)
    c = result.name_cost.sum(axis=0)
    t = result.name_turnover.sum(axis=0)
    w = np.abs(result.weight_matrix).mean(axis=0)
    df = pd.DataFrame({
        "inst": result.insts, "gross_pnl": g, "cost": c,
        "net_pnl": g - c, "turnover": t, "avg_abs_weight": w,
    })
    tot = df["net_pnl"].sum()
    df["share_of_net"] = df["net_pnl"] / tot if tot != 0 else np.nan
    df = df.sort_values("net_pnl", ascending=False)
    df["cum_share"] = df["share_of_net"].cumsum()
    return df.reset_index(drop=True)


def concentration_stats(result: BacktestResult) -> Dict[str, float]:
    df = coin_attribution(result)
    pos = df[df["net_pnl"] > 0]
    tot = df["net_pnl"].sum()
    if tot <= 0:
        return {"top1_share": np.nan, "top5_share": np.nan, "herfindahl": np.nan,
                "n_positive": int((df["net_pnl"] > 0).sum()),
                "n_negative": int((df["net_pnl"] < 0).sum())}
    return {
        "top1_share": float(df["net_pnl"].iloc[0] / tot),
        "top5_share": float(df["net_pnl"].iloc[:5].sum() / tot),
        "herfindahl": float(((df["net_pnl"].clip(lower=0) / max(pos["net_pnl"].sum(), 1e-9)) ** 2).sum()),
        "n_positive": int((df["net_pnl"] > 0).sum()),
        "n_negative": int((df["net_pnl"] < 0).sum()),
    }


def btc_eth_dependence(result: BacktestResult) -> pd.DataFrame:
    """How much of the P&L would remain if BTC and ETH were excluded from trading?"""
    df = coin_attribution(result)
    tot = df["net_pnl"].sum()
    mask = df["inst"].isin(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    sub = df[mask]["net_pnl"].sum()
    return pd.DataFrame([
        {"scenario": "all", "net_pnl": tot},
        {"scenario": "ex-BTC", "net_pnl": tot - df[df.inst == "BTC-USDT-SWAP"]["net_pnl"].sum()},
        {"scenario": "ex-ETH", "net_pnl": tot - df[df.inst == "ETH-USDT-SWAP"]["net_pnl"].sum()},
        {"scenario": "ex-BTC+ETH", "net_pnl": tot - sub},
    ])
