"""All research charts.  Light theme, colour-blind-safe palette, no chartjunk."""
from __future__ import annotations

import os
from typing import Dict, Optional, Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    import seaborn as sns
    sns.set_theme(style="whitegrid", context="notebook")
except Exception:  # noqa: BLE001
    pass

plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white",
    "axes.edgecolor": "#cfd8e3", "axes.labelcolor": "#1f2933",
    "text.color": "#1f2933", "xtick.color": "#52606d", "ytick.color": "#52606d",
    "grid.color": "#e4e7eb", "axes.titlesize": 12,
    "figure.dpi": 110, "savefig.bbox": "tight", "font.size": 9.5,
})

C = {
    "main": "#1f6feb", "alt": "#8957e5", "long": "#c0392b", "short": "#0e8a6a",
    "cost": "#b7791f", "grey": "#8a94a6", "bench": "#6b7280", "accent": "#d9480f",
}


def _save(fig, outdir: str, name: str) -> str:
    os.makedirs(outdir, exist_ok=True)
    p = os.path.join(outdir, name)
    fig.savefig(p)
    plt.close(fig)
    return p


def _fin(ax, title: str, ylabel: str = "", xlabel: str = ""):
    ax.set_title(title)
    if ylabel:
        ax.set_ylabel(ylabel)
    if xlabel:
        ax.set_xlabel(xlabel)
    ax.margins(x=0.01)


# ---------------------------------------------------------------------------
def equity_curve(bars: pd.DataFrame, outdir: str, extra: Dict[str, pd.Series] | None = None,
                 name: str = "01_equity_curve.png") -> str:
    fig, ax = plt.subplots(figsize=(11, 4.6))
    ax.plot(bars.index, bars["equity"], color=C["main"], lw=1.4, label="Strategy (net)")
    gross = (1 + bars["gross_ret"]).cumprod()
    ax.plot(bars.index, gross, color=C["grey"], lw=1.0, ls="--", label="Gross (pre-cost)")
    if extra:
        for k, s in extra.items():
            ax.plot(s.index, s, lw=1.0, label=k)
    ax.set_yscale("log")
    _fin(ax, "Equity Curve (log scale)", "Growth of $1")
    ax.legend(loc="upper left", frameon=False)
    return _save(fig, outdir, name)


def long_short_equity(bars: pd.DataFrame, outdir: str,
                      name: str = "02_long_short_equity.png") -> str:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    l = (1 + bars["long_ret"]).cumprod()
    s = (1 + bars["short_ret"]).cumprod()
    axes[0].plot(bars.index, l, color=C["long"], lw=1.3, label="Long leg")
    axes[0].plot(bars.index, s, color=C["short"], lw=1.3, label="Short leg")
    axes[0].plot(bars.index, bars["equity"], color=C["main"], lw=1.5, label="Net")
    axes[0].set_yscale("log")
    _fin(axes[0], "Long / Short / Net equity", "Growth of $1")
    axes[0].legend(frameon=False)
    contrib = pd.Series({"Long": bars["long_ret"].sum(), "Short": bars["short_ret"].sum(),
                         "Fees": -bars["fee"].sum(), "Spread": -bars["spread"].sum(),
                         "Impact": -bars["impact"].sum(), "Funding": -bars["funding"].sum()})
    colors = [C["long"], C["short"], C["cost"], C["cost"], C["cost"], C["alt"]]
    axes[1].bar(contrib.index, contrib.values, color=colors)
    axes[1].axhline(0, color="#9aa5b1", lw=0.8)
    _fin(axes[1], "P&L contribution decomposition", "cumulative return")
    plt.setp(axes[1].get_xticklabels(), rotation=20, ha="right")
    return _save(fig, outdir, name)


def drawdown_curve(bars: pd.DataFrame, outdir: str, name: str = "03_drawdown.png") -> str:
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.fill_between(bars.index, bars["drawdown"] * 100, 0, color=C["accent"], alpha=0.28)
    ax.plot(bars.index, bars["drawdown"] * 100, color=C["accent"], lw=0.9)
    _fin(ax, "Drawdown", "%")
    return _save(fig, outdir, name)


def monthly_heatmap(mret: pd.DataFrame, outdir: str,
                    name: str = "04_monthly_returns.png") -> str:
    fig, ax = plt.subplots(figsize=(9.5, 0.52 * max(len(mret), 3) + 1.6))
    v = float(np.nanmax(np.abs(mret.to_numpy()))) if mret.size else 0.1
    v = max(v, 0.02)
    sns.heatmap(mret * 100, annot=True, fmt=".1f", cmap="RdYlGn_r", center=0,
                vmin=-v * 100, vmax=v * 100, ax=ax, cbar_kws={"label": "%"},
                linewidths=0.4, linecolor="white")
    ax.set_title("Monthly net returns (%)")
    ax.set_xlabel("month")
    ax.set_ylabel("year")
    return _save(fig, outdir, name)


def rolling_panels(roll: pd.DataFrame, bars: pd.DataFrame, outdir: str,
                   name: str = "05_rolling.png") -> str:
    fig, axes = plt.subplots(2, 2, figsize=(12, 6.2))
    axes[0, 0].plot(roll.index, roll["roll_sharpe"], color=C["main"], lw=1.2)
    axes[0, 0].axhline(0, color="#9aa5b1", lw=0.8)
    _fin(axes[0, 0], "Rolling Sharpe (90d)", "")
    axes[0, 1].plot(roll.index, roll["roll_ann_vol"] * 100, color=C["alt"], lw=1.2)
    _fin(axes[0, 1], "Rolling annualised volatility", "%")
    axes[1, 0].plot(roll.index, roll["roll_beta"], color=C["accent"], lw=1.2)
    axes[1, 0].axhline(0, color="#9aa5b1", lw=0.8)
    _fin(axes[1, 0], "Rolling BTC beta exposure", "")
    d = bars["turnover"].resample("1D").sum()
    axes[1, 1].plot(d.index, d.rolling(30).mean() * 100, color=C["cost"], lw=1.2)
    _fin(axes[1, 1], "Rolling 30d daily turnover", "% of gross")
    fig.tight_layout()
    return _save(fig, outdir, name)


def cost_curves(bars: pd.DataFrame, outdir: str, name: str = "06_cost_curves.png") -> str:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.0))
    for col, lab, c in (("fee", "fee", C["cost"]), ("spread", "spread", C["alt"]),
                        ("impact", "impact/slippage", C["accent"])):
        axes[0].plot(bars.index, bars[col].cumsum() * 100, lw=1.2, color=c, label=lab)
    _fin(axes[0], "Cumulative trading cost", "% of equity")
    axes[0].legend(frameon=False)
    f = bars["funding"].resample("1ME").sum() * 100
    axes[1].bar(f.index, f.values, width=22, color=np.where(f.values >= 0, C["short"], C["long"]))
    axes[1].axhline(0, color="#9aa5b1", lw=0.8)
    _fin(axes[1], "Monthly funding P&L (+ = received)", "% of equity")
    return _save(fig, outdir, name)


def ic_charts(ic_tab: pd.DataFrame, decay: pd.DataFrame, factor_ic: pd.DataFrame,
              outdir: str, name: str = "07_ic.png") -> str:
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.0))
    score = ic_tab[ic_tab["factor"] == "SCORE"] if "factor" in ic_tab.columns else ic_tab
    axes[0].bar([f"{h:g}d" for h in score["horizon_days"]], score["IC_mean"], color=C["main"])
    axes[0].axhline(0, color="#9aa5b1", lw=0.8)
    plt.setp(axes[0].get_xticklabels(), rotation=30)
    _fin(axes[0], "Score Rank-IC by horizon", "mean IC")

    if "factor" in factor_ic.columns:
        piv = factor_ic.pivot_table(index="factor", columns="horizon_days", values="IC_mean")
        sns.heatmap(piv, annot=True, fmt=".3f", cmap="RdBu_r", center=0, ax=axes[1],
                    linewidths=0.4, linecolor="white", cbar_kws={"label": "IC"})
        axes[1].set_title("Rank-IC: factor x horizon")
        axes[1].set_xlabel("horizon (days)")
        axes[1].set_ylabel("")

    axes[2].plot(decay["horizon_days"], decay["IC_mean"], color=C["accent"], lw=1.5,
                 marker="o", ms=3)
    axes[2].axhline(0, color="#9aa5b1", lw=0.8)
    _fin(axes[2], "IC decay", "mean IC", "horizon (days)")
    fig.tight_layout()
    return _save(fig, outdir, name)


def sensitivity_heatmap(grid: pd.DataFrame, outdir: str, title: str,
                        name: str = "08_sensitivity.png") -> str:
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    sns.heatmap(grid, annot=True, fmt=".2f", cmap="RdYlGn", center=0, ax=ax,
                linewidths=0.5, linecolor="white")
    ax.set_title(title)
    return _save(fig, outdir, name)


def capacity_curve(cap: pd.DataFrame, outdir: str, name: str = "09_capacity.png") -> str:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.0))
    axes[0].semilogx(cap["capital"], cap["Sharpe"], marker="o", color=C["main"], lw=1.4)
    _fin(axes[0], "Sharpe vs AUM", "Sharpe", "AUM (USD, log)")
    axes[1].semilogx(cap["capital"], cap["slippage"] * 100, marker="o", color=C["accent"],
                     lw=1.4, label="impact/slippage")
    axes[1].semilogx(cap["capital"], cap["fee"] * 100, marker="s", color=C["cost"],
                     lw=1.4, label="fees")
    axes[1].semilogx(cap["capital"], cap["spread"] * 100, marker="^", color=C["alt"],
                     lw=1.4, label="spread")
    _fin(axes[1], "Cumulative cost vs AUM", "% of equity", "AUM (USD, log)")
    axes[1].legend(frameon=False)
    fig.tight_layout()
    return _save(fig, outdir, name)


def attribution_charts(ls: pd.DataFrame, fabl: pd.DataFrame, regime: pd.DataFrame,
                       outdir: str, name: str = "10_attribution.png") -> str:
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.2))
    x = np.arange(len(ls))
    w = 0.36
    axes[0].bar(x - w / 2, ls["CAGR"] * 100, w, color=C["main"], label="CAGR %")
    axes[0].bar(x + w / 2, ls["Sharpe"] * 100, w, color=C["alt"], label="Sharpe x100")
    axes[0].set_xticks(x)
    axes[0].set_xticklabels(ls["leg"])
    axes[0].axhline(0, color="#9aa5b1", lw=0.8)
    axes[0].legend(frameon=False)
    _fin(axes[0], "Long / Short / Net attribution")

    f = fabl.sort_values("Sharpe", ascending=True)
    axes[1].barh(f["run"], f["Sharpe"], color=C["main"])
    axes[1].axvline(0, color="#9aa5b1", lw=0.8)
    _fin(axes[1], "Factor ablation (Sharpe)", "")
    plt.setp(axes[1].get_yticklabels(), fontsize=7.5)

    r = regime[regime["dimension"] == "trend"]
    axes[2].bar(r["regime"], r["contrib_to_total"] * 100, color=C["accent"])
    axes[2].axhline(0, color="#9aa5b1", lw=0.8)
    _fin(axes[2], "P&L by BTC market regime", "contribution %")
    fig.tight_layout()
    return _save(fig, outdir, name)


def regime_heatmap(regime: pd.DataFrame, outdir: str,
                   name: str = "11_regime_heatmap.png") -> str:
    piv = regime.pivot_table(index="regime", columns="dimension",
                             values="contrib_to_total") * 100
    fig, ax = plt.subplots(figsize=(8.5, 0.45 * len(piv) + 1.8))
    sns.heatmap(piv, annot=True, fmt=".1f", cmap="RdYlGn_r", center=0, ax=ax,
                linewidths=0.4, linecolor="white", cbar_kws={"label": "% contribution"})
    ax.set_title("Regime attribution (% of cumulative net return)")
    return _save(fig, outdir, name)


def montecarlo_dist(real: float, placebo: Sequence[float], title: str, outdir: str,
                    name: str) -> str:
    v = np.asarray([x for x in placebo if np.isfinite(x)])
    fig, ax = plt.subplots(figsize=(7.6, 3.8))
    if v.size:
        ax.hist(v, bins=min(30, max(8, v.size // 3)), color=C["grey"], alpha=0.75,
                label=f"placebo (n={v.size})")
        ax.axvline(np.mean(v), color="#52606d", ls=":", lw=1.1, label=f"placebo mean {np.mean(v):.2f}")
    ax.axvline(real, color=C["main"], lw=2.0, label=f"strategy {real:.2f}")
    _fin(ax, title, "count", "Sharpe")
    ax.legend(frameon=False)
    return _save(fig, outdir, name)


def coin_bar(coin_df: pd.DataFrame, outdir: str, top: int = 20,
             name: str = "12_coin_attribution.png") -> str:
    d = pd.concat([coin_df.head(top), coin_df.tail(top)])
    fig, ax = plt.subplots(figsize=(9, 0.28 * len(d) + 1.8))
    colors = np.where(d["net_pnl"] >= 0, C["long"], C["short"])
    ax.barh(d["inst"], d["net_pnl"] * 100, color=colors)
    ax.axvline(0, color="#9aa5b1", lw=0.8)
    _fin(ax, f"Per-coin net P&L (top/bottom {top})", "% of equity")
    ax.invert_yaxis()
    return _save(fig, outdir, name)


def turnover_frequency_chart(tab: pd.DataFrame, outdir: str,
                             name: str = "13_frequency_tradeoff.png") -> str:
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.0))
    axes[0].plot(tab["ann_turnover"], tab["Sharpe"], "o", color=C["main"])
    for _, r in tab.iterrows():
        axes[0].annotate(r["label"], (r["ann_turnover"], r["Sharpe"]), fontsize=6.5)
    _fin(axes[0], "Sharpe vs annual turnover", "Sharpe", "annual turnover (x gross)")
    axes[1].plot(tab["ann_turnover"], -tab["cost_drag"] * 100, "o", color=C["cost"])
    _fin(axes[1], "Cost drag vs turnover", "annual cost %", "annual turnover")
    axes[2].bar(tab["label"], tab["Sharpe"], color=C["alt"])
    axes[2].axhline(0, color="#9aa5b1", lw=0.8)
    _fin(axes[2], "Sharpe by rebalance frequency", "Sharpe")
    plt.setp(axes[2].get_xticklabels(), rotation=30, ha="right", fontsize=7.5)
    fig.tight_layout()
    return _save(fig, outdir, name)
