"""Blind test: is the book's P&L the *strategy*, or is the *market* just good?

"Market good" can mean three different things, and they need three different
tests -- running only the first is how a beta book gets mistaken for an alpha
book:

1. **BETA.**  Is the return just leveraged crypto exposure?  Regress the book's
   per-bar return on a market factor and look at beta, R^2 and the intercept.
   The accepted book is dollar-neutral by construction (|net_exposure| ~ 0,
   `beta_exposure` mean ~ -0.001), so this *should* come back with beta ~ 0.
   That is exactly why it cannot be the whole answer: a construction that pins
   beta to zero makes beta a tautology, not a finding.

2. **LUCK.**  Does the *ranking* carry information, or would any ranking do?
   The Monte-Carlo nulls permute the score three ways (cross-section, random
   score, block permutation).  A book whose ranking is noise reproduces the same
   P&L when the score is permuted.  This is the only one of the three that can
   actually falsify the strategy.

3. **DIRECTION.**  A market-driven long/short book earns everything from the
   long leg -- it is long beta with extra steps.  So split the P&L by leg and by
   regime (bull / sideways / bear): a book that only works in `Bull` is a
   market-timing story wearing a market-neutral costume.

Run:  python scripts/exp_market_vs_strategy.py [--tag v5_1d_all5] [--compare v4_1d]
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

from crypto_ls_research.data.asset_class import (                         # noqa: E402
    filter_insts, load_categories)
from crypto_ls_research.data.store import list_cached_insts, load_panels  # noqa: E402

BAR = "1h"
START, END = "2021-01-01", "2026-09-26"
BARS_PER_YEAR = 24 * 365
BARS_PER_DAY = 24
ART = os.path.join(ROOT, "artifacts")


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def _bars(tag: str) -> pd.DataFrame:
    path = os.path.join(ART, tag, "baseline.pkl")
    with open(path, "rb") as f:
        res = pickle.load(f)["result"]
    b = res.bars.copy()
    # The warmup is flat zeros, not returns: including it would bias every mean
    # toward zero and inflate the R^2 of any regression that has a warmup too.
    live = b["gross_exposure"] > 0
    if live.any():
        b = b.loc[live.idxmax():]
    return b


def _market(bar: str) -> pd.DataFrame:
    """BTC, an equal-weight crypto index, and the same excluding BTC/ETH.

    Scoped exactly like the acceptance run (`--asset-class crypto`) so the
    "market" is the market the book actually trades in, not OKX's tokenised
    equities.
    """
    insts = filter_insts(list_cached_insts(bar), load_categories(), "crypto")[0]
    p = load_panels(bar, START, END, insts=insts, extend_to_last=True)
    ret = p.close.pct_change()
    out = pd.DataFrame(index=ret.index)
    if "BTC-USDT-SWAP" in ret.columns:
        out["btc"] = ret["BTC-USDT-SWAP"]
    out["ew"] = ret.mean(axis=1)
    alt = [c for c in ret.columns if c not in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")]
    out["ew_ex_btc_eth"] = ret[alt].mean(axis=1)
    return out


# ---------------------------------------------------------------------------
# OLS with Newey-West (no statsmodels in this env, and one regressor does not
# justify adding one)
# ---------------------------------------------------------------------------
def _ols_nw(y: np.ndarray, x: np.ndarray, lags: int) -> dict:
    n = y.size
    X = np.column_stack([np.ones(n), x])
    xtx_inv = np.linalg.inv(X.T @ X)
    coef = xtx_inv @ (X.T @ y)
    resid = y - X @ coef

    # HAC (Bartlett kernel).  The book holds positions across bars, so the
    # residuals are strongly autocorrelated and a plain OLS t-stat would be
    # several times too large -- that is the whole reason this is here.
    #
    # Var(beta) = (X'X)^-1 * [sum_t u_t u_t'] * (X'X)^-1, so the meat is the
    # *unscaled* outer-product sum.  Dividing it by n as well (and then by n
    # again below) shrinks every standard error by sqrt(n) and inflates the
    # t-stats by ~45x on this sample -- which is how a first version of this
    # script "found" a t-stat of 10,109.
    u = X * resid[:, None]
    S = u.T @ u
    for L in range(1, lags + 1):
        w = 1.0 - L / (lags + 1.0)
        G = u[L:].T @ u[:-L]
        S += w * (G + G.T)
    cov = xtx_inv @ S @ xtx_inv
    se = np.sqrt(np.diag(cov))

    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else np.nan
    return {"alpha": float(coef[0]), "beta": float(coef[1]),
            "t_alpha": float(coef[0] / se[0]), "t_beta": float(coef[1] / se[1]),
            "r2": r2, "n": int(n), "lags": int(lags)}


def _ann(per_bar: float, ppy: int) -> float:
    """Annualise a per-bar rate.  `ppy` must match the frequency of the series:
    raising a *daily* alpha to the power 8760 reports 5,111 (i.e. +511,100%)
    where the truth is 0.43."""
    return (1.0 + per_bar) ** ppy - 1.0


def _sharpe(r: pd.Series, ppy: int) -> float:
    s = r.std(ddof=1)
    return float(r.mean() / s * np.sqrt(ppy)) if s and s > 0 else np.nan


def _mdd(eq: pd.Series) -> float:
    return float((eq / eq.cummax() - 1.0).min())


# ---------------------------------------------------------------------------
def regress(tag: str, bars: pd.DataFrame, mkt: pd.DataFrame,
            freq: str) -> pd.DataFrame:
    """`freq` is 'hourly' or 'daily' -- daily is the rebalance frequency, so it
    is the honest horizon; hourly keeps the sample size honest."""
    y = bars[["net_ret", "gross_ret", "long_ret", "short_ret"]].copy()
    df = y.join(mkt, how="inner").dropna(subset=["btc", "ew", "ew_ex_btc_eth"])
    if freq == "daily":
        df = (1.0 + df).resample("1D").prod() - 1.0
        df = df.dropna(how="all")
        lags = 5
        ppy = 365
    else:
        lags = BARS_PER_DAY
        ppy = BARS_PER_YEAR

    rows = []
    for col in ("net_ret", "gross_ret", "long_ret", "short_ret"):
        for factor in ("btc", "ew", "ew_ex_btc_eth"):
            d = df[[col, factor]].dropna()
            if d.shape[0] < 100:
                continue
            r = _ols_nw(d[col].to_numpy(), d[factor].to_numpy(), lags)
            rows.append({
                "tag": tag, "freq": freq, "series": col, "factor": factor,
                "n": r["n"], "lags": r["lags"],
                "alpha_bar": r["alpha"], "alpha_ann": _ann(r["alpha"], ppy),
                "t_alpha_nw": r["t_alpha"], "beta": r["beta"],
                "t_beta_nw": r["t_beta"], "r2": r["r2"],
                "corr": float(d[col].corr(d[factor])),
            })
    return pd.DataFrame(rows)


def leg_and_regime(tag: str, bars: pd.DataFrame, mkt: pd.DataFrame) -> dict:
    d = bars[["net_ret", "gross_ret", "long_ret", "short_ret",
              "beta_exposure", "net_exposure", "gross_exposure"]].join(
                  mkt, how="inner").dropna(subset=["btc"])
    up, dn = d["btc"] > 0, d["btc"] <= 0
    out = {
        "tag": tag,
        "n_bars": int(d.shape[0]),
        "beta_exp_mean": float(d["beta_exposure"].mean()),
        "beta_exp_mean_abs": float(d["beta_exposure"].abs().mean()),
        "net_exp_mean_abs": float(d["net_exposure"].abs().mean()),
        "gross_exp_mean": float(d["gross_exposure"].mean()),
        "long_share_of_gross_pnl": float(d["long_ret"].sum() /
                                         (d["long_ret"].sum() + d["short_ret"].sum())),
    }
    for nm, mask in (("btc_up", up), ("btc_down", dn)):
        sub = d.loc[mask]
        # A *conditional* subset has no meaningful "annualised return": its bars
        # are selected by the sign of the market's move, so compounding them
        # measures a selection, not a return (it comes out `inf`).  The mean bar
        # return and the conditional Sharpe are the quantities that survive.
        out[f"{nm}_bars"] = int(sub.shape[0])
        out[f"{nm}_net_mean_bar"] = float(sub["net_ret"].mean())
        out[f"{nm}_net_sharpe"] = _sharpe(sub["net_ret"], BARS_PER_YEAR)
        out[f"{nm}_btc_mean_bar"] = float(sub["btc"].mean())
    return out


def bench_buy_hold(mkt: pd.DataFrame) -> dict:
    """What "the market was just good" looks like as a tradeable alternative."""
    out = {}
    for k in ("btc", "ew"):
        if k not in mkt:
            continue
        r = mkt[k].dropna()
        eq = (1.0 + r).cumprod()
        out[f"{k}_cagr"] = float(eq.iloc[-1] ** (BARS_PER_YEAR / r.size) - 1.0)
        out[f"{k}_sharpe"] = _sharpe(r, BARS_PER_YEAR)
        out[f"{k}_mdd"] = _mdd(eq)
        out[f"{k}_ann_vol"] = float(r.std(ddof=1) * np.sqrt(BARS_PER_YEAR))
    return out


def mc_nulls(tag: str) -> dict:
    p = os.path.join(ART, tag, "tables", "22_montecarlo_summary.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def bandwidth_scan(tag: str, bars: pd.DataFrame, mkt: pd.DataFrame) -> pd.DataFrame:
    """The verdict must not depend on the HAC bandwidth.

    A single t-stat can always be argued away as bandwidth-shopping, and the
    estimator is only validated for a *range* of lags (checked against a moving
    block bootstrap on synthetic AR(1) data: `se_hac/se_boot` is 0.41 at
    lags=0, 0.78 at 5, **1.00** at 24, 1.03 at 48 -- i.e. anything below ~24
    lags is too small on this kind of residual).  So report the whole curve.
    """
    d = bars[["net_ret", "gross_ret"]].join(mkt, how="inner").dropna(
        subset=["ew"])
    d = (1.0 + d).resample("1D").prod() - 1.0
    d = d.dropna(how="all")

    rows = []
    for col in ("net_ret", "gross_ret"):
        for lags in (0, 2, 5, 10, 21, 48):
            r = _ols_nw(d[col].to_numpy(), d["ew"].to_numpy(), lags)
            rows.append({"tag": tag, "series": col, "factor": "ew",
                         "lags": lags, "alpha_ann": _ann(r["alpha"], 365),
                         "t_alpha_nw": r["t_alpha"], "beta": r["beta"],
                         "t_beta_nw": r["t_beta"], "r2": r["r2"]})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v5_1d_all5")
    ap.add_argument("--compare", default="")
    args = ap.parse_args()

    tags = [args.tag] + ([args.compare] if args.compare else [])
    mkt = _market(BAR)

    frames, legs = [], []
    for tag in tags:
        b = _bars(tag)
        frames.append(regress(tag, b, mkt, "daily"))
        frames.append(regress(tag, b, mkt, "hourly"))
        legs.append(leg_and_regime(tag, b, mkt))
    reg = pd.concat(frames, ignore_index=True)
    leg = pd.DataFrame(legs)

    bw = pd.concat([bandwidth_scan(t, _bars(t), mkt) for t in tags],
                   ignore_index=True)
    bw.to_csv(os.path.join(ART, args.tag, "tables", "35c_market_bandwidth.csv"),
              index=False)

    os.makedirs(os.path.join(ART, args.tag, "tables"), exist_ok=True)
    reg.to_csv(os.path.join(ART, args.tag, "tables", "35_market_regression.csv"),
               index=False)
    leg.to_csv(os.path.join(ART, args.tag, "tables", "35b_market_decomposition.csv"),
               index=False)

    pd.set_option("display.width", 200)
    print("=" * 96)
    print("1) BETA -- per-bar return regressed on the market, Newey-West HAC")
    print("=" * 96)
    show = reg[reg["freq"] == "daily"][
        ["tag", "series", "factor", "n", "alpha_ann", "t_alpha_nw",
         "beta", "t_beta_nw", "r2", "corr"]]
    print(show.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    print("\n(hourly, net_ret only)")
    print(reg[(reg["freq"] == "hourly") & (reg["series"] == "net_ret")][
        ["tag", "factor", "n", "alpha_ann", "t_alpha_nw", "beta", "r2", "corr"]
    ].to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    print("\n(bandwidth scan, daily, equal-weight market -- the verdict must not "
          "depend on the lag choice)")
    print(bw.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    print("\n" + "=" * 96)
    print("2) LUCK -- Monte-Carlo placebo nulls (the only falsifiable test here)")
    print("=" * 96)
    for tag in tags:
        mc = mc_nulls(tag)
        if not mc:
            print(f"  {tag}: 22_montecarlo_summary.json 不在盘上（mc 阶段未完成）")
            continue
        for kind, d in mc.items():
            p = (d.get("sharpe") or {}).get("p_value")
            real = (d.get("sharpe") or {}).get("real")
            pct = (d.get("sharpe") or {}).get("pct")
            print(f"  {tag:12s} {kind:24s} p={p}  real={real}  pct={pct}")

    print("\n" + "=" * 96)
    print("3) DIRECTION -- leg split, exposure, and bull/bear behaviour")
    print("=" * 96)
    print(leg.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    print("\n" + "=" * 96)
    print("4) What 'the market was good' would have paid (same window, buy & hold)")
    print("=" * 96)
    bh = bench_buy_hold(mkt)
    for k, v in bh.items():
        print(f"  {k:24s} {v:,.4f}")

    print("\n产物：tables/35_market_regression.csv, tables/35b_market_decomposition.csv")


if __name__ == "__main__":
    main()
