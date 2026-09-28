"""Single-loop backtest engine.

Timing protocol (the part that kills most crypto backtests)
-----------------------------------------------------------
    decision bar  d        : signal computed from bars <= d  (at the close of d)
    signal        S[d]
    order         d+1      : submitted at the start of bar d+1
    execution px  px[d+1]  : the chosen execution price *inside* bar d+1
    holding       [d+1, d+2]: marked out at px[d+2]

In code the position vector `held[t]` is built from `S[t-1]`, and it earns
`px[t+1]/px[t] - 1`.  There is no code path in which a signal is filled at the price
of its own bar.  Risk overlays (vol targeting, regime, drawdown ladder, turnover
budget) are all evaluated with `equity` / `drawdown` as of the decision bar.

Stale-data convention
---------------------
If an instrument's price disappears while held, the missing interval is marked flat
(zero P&L) and the position is force-closed, paying the taker fee + widest spread.
This is the conservative treatment; the count of such events is reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..config.settings import BacktestConfig
from ..data.store import Panels, panels_to_arrays
from ..factors.engine import FACTOR_NAMES, compute_factors
from ..portfolio.beta_neutral import rolling_beta, side_beta, side_gross_targets
from ..portfolio.construct import Selection, build_units, select_book
from ..risk import engine as risk
from ..signals.cross_section import composite_score, residualize, zscore
from ..universe.pit import listing_age_bars, min_history_bars, universe_mask
from .costs import trade_cost

EPS = 1e-12


@dataclass
class BacktestResult:
    insts: List[str]
    bars: pd.DataFrame
    rebalances: List[dict]
    reb_ts: pd.DatetimeIndex
    score_matrix: np.ndarray           # (D, N) NaN outside PIT pool
    mask_matrix: np.ndarray            # (D, N) bool
    beta_matrix: np.ndarray            # (D, N)
    adv_matrix: np.ndarray             # (D, N)
    factor_zs: Dict[str, np.ndarray]   # (D, N)
    fc_raw: Dict[str, np.ndarray]      # (D, N) raw factor values
    weight_matrix: np.ndarray          # (D, N) signed weights in force during each period
    name_gross: np.ndarray             # (D, N) gross price P&L per instrument per period
    name_cost: np.ndarray              # (D, N) fee+spread+impact per instrument
    name_turnover: np.ndarray          # (D, N) traded weight per instrument
    name_fund: np.ndarray              # (D, N) funding CASHFLOW per instrument per period
    cfg: BacktestConfig
    meta: Dict = field(default_factory=dict)

    @property
    def equity(self) -> pd.Series:
        return (1.0 + self.bars["net_ret"]).cumprod().rename("equity")

    @property
    def fund_matrix(self) -> np.ndarray:
        """`name_fund` with a zero fallback for pickles written before it existed.

        Unpickling restores `__dict__` directly, so a dataclass default does not
        help an old object -- it simply lacks the attribute.  Anything that
        attributes funding per instrument (the trade-record view) must not blow up
        on those; it gets zeros and can tell via `meta["has_name_fund"]`.
        """
        a = getattr(self, "name_fund", None)
        if a is None:
            return np.zeros(np.asarray(self.name_gross).shape, dtype="float64")
        return np.asarray(a, dtype="float64")


# ---------------------------------------------------------------------------
def pick_exec_price(arr: Dict[str, np.ndarray], mode: str) -> np.ndarray:
    if mode == "next_open":
        return arr["open"]
    if mode == "next_close":
        return arr["close"]
    if mode == "next_vwap":
        with np.errstate(divide="ignore", invalid="ignore"):
            v = arr["amount"] / arr["vol_ccy"]
        return np.where(np.isfinite(v) & (v > 0), v, np.nan).astype("float32")
    if mode == "next_twap":
        return ((arr["high"].astype("float64") + arr["low"] + arr["close"]) / 3.0).astype("float32")
    raise ValueError(f"unknown exec_price mode: {mode}")


def _ffill_rows(a: np.ndarray) -> np.ndarray:
    """Forward-fill NaN along axis 0.  Used ONLY for marking stale positions flat."""
    a = np.asarray(a, dtype="float64")
    n, m = a.shape
    mask = np.isfinite(a)
    if mask.all():
        return a
    idx = np.where(mask, np.arange(n)[:, None], 0).astype("int64")
    np.maximum.accumulate(idx, axis=0, out=idx)
    out = a[idx, np.arange(m)[None, :]]
    first = np.argmax(mask, axis=0)
    rows = np.arange(n)[:, None]
    out = np.where(rows >= first[None, :], out, np.nan)
    out[:, ~mask.any(axis=0)] = np.nan
    return out


# ---------------------------------------------------------------------------
def run_backtest(panels: Panels, cfg: BacktestConfig,
                 factor_subset: Optional[Sequence[str]] = None,
                 factor_profile: Optional[Sequence[float]] = None,
                 selection_mode: str = "factor",
                 score_permute: str = "none",
                 rng: Optional[np.random.Generator] = None,
                 randomize_score: bool = False,
                 disable_risk_overlays: bool = False,
                 disable_costs: bool = False,
                 extra_gross: Optional[np.ndarray] = None,
                 dec_offset_bars: int = 0) -> BacktestResult:
    """Run one backtest.  Pure function of (panels, cfg, flags).

    `factor_subset=None` falls back to `cfg.factors.subset`, so a factor-set
    change made through a config override reaches *every* backtest in a sweep
    instead of only the ones that explicitly pass the kwarg.

    `dec_offset_bars` shifts the whole rebalance grid later by that many bars
    (`dec_idx = arange(warmup + k, T-1, R)`), moving BOTH the signal bar and
    the execution bar.  The default 0 reproduces the anchored-at-UTC-02:00
    grid bit-for-bit, which is what every archived result used.  It exists so
    a grid-phase question ("place orders at 23:30 Beijing instead") can be
    answered with a measurement instead of an opinion --- see
    `scripts/exp_grid_phase.py`.
    """
    if factor_subset is None:
        factor_subset = cfg.factors.subset
    factor_subset = tuple(factor_subset)
    unknown = [f for f in factor_subset if f not in FACTOR_NAMES]
    if unknown:
        raise ValueError(f"unknown factor(s) in subset: {unknown}; known: {FACTOR_NAMES}")
    if not factor_subset:
        raise ValueError("factor_subset must not be empty")
    if factor_profile is None:
        factor_profile = cfg.factors.profiles[cfg.factors.default_profile]
    wprof = np.asarray(factor_profile, dtype="float64")
    rng = rng or np.random.default_rng(20260926)

    arr = panels_to_arrays(panels)
    insts = panels.insts
    ts = panels.index
    N, T = len(insts), len(panels.index)
    bpd, bpy = cfg.bars_per_day, cfg.bars_per_year
    R = max(1, int(cfg.rebalance_bars))

    # ---- execution price & per-bar returns ---------------------------------
    px = pick_exec_price(arr, cfg.execution.exec_price)
    px_ff = _ffill_rows(px)
    ret_exec = np.zeros((T, N), dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        ret_exec[:-1] = px_ff[1:] / px_ff[:-1] - 1.0
    ret_exec = np.nan_to_num(ret_exec, nan=0.0, posinf=0.0, neginf=0.0)
    alive = np.isfinite(px)
    close_ff = _ffill_rows(arr["close"].astype("float64"))

    # ---- decision bars -----------------------------------------------------
    warmup = max(
        min_history_bars(cfg.universe, bpd) + 2,
        int(round(cfg.factors.flow_long_days * bpd)) + 2,
        int(round(cfg.factors.range_days * bpd)) + 2,
        int(round(cfg.risk.vol_est_window_days * bpd)) + 2,
    )
    dec_idx = np.arange(warmup + int(dec_offset_bars), T - 1, R, dtype=int)
    D = dec_idx.size
    if D < 3:
        raise ValueError(f"not enough bars: T={T} warmup={warmup} R={R} -> D={D}")

    # ---- trailing factors --------------------------------------------------
    # Sub-sample to the decision bars immediately and free the full panels; a
    # 50k x 161 x 7 float64 factor stack is ~450 MB and would multiply across
    # sweep worker processes.
    fac = compute_factors(panels, cfg.factors, cfg.universe, bpd, bpy)
    raw_d = {k: fac[k].to_numpy(dtype="float64")[dec_idx] for k in FACTOR_NAMES}
    vol_d = fac["vol"].to_numpy(dtype="float64")[dec_idx]
    atr_d = fac["atr_pct"].to_numpy(dtype="float64")[dec_idx]
    adv_d = fac["adv"].to_numpy(dtype="float64")[dec_idx]
    del fac
    # The eligibility gate must see RAW prices: a forward-filled price would let a
    # stalled instrument stay in the tradable pool.
    close_d = arr["close"].astype("float64")[dec_idx]
    daily_vol_d = np.where(np.isfinite(vol_d), vol_d / np.sqrt(bpy), np.nan)

    # ---- beta vs benchmark -------------------------------------------------
    bench = cfg.benchmark_inst
    if bench not in insts:
        raise ValueError(f"benchmark {bench} not in panel columns")
    ret_cc = panels.close.pct_change()
    bw = max(10, int(round(cfg.portfolio.beta_lookback_days * bpd)))
    beta_arr = rolling_beta(ret_cc, ret_cc[bench], bw).reindex(columns=insts) \
        .to_numpy(dtype="float64")
    beta_d = beta_arr[dec_idx]

    # ---- listing age -------------------------------------------------------
    first_valid = np.array([
        (np.flatnonzero(np.isfinite(panels.close[c].to_numpy(dtype="float64")))[0]
         if panels.close[c].notna().any() else -1) for c in insts], dtype="float64")
    age_d = listing_age_bars(first_valid, T)[dec_idx]
    min_hist = min_history_bars(cfg.universe, bpd)

    # ---- BTC regime (trailing) --------------------------------------------
    btc_mom, btc_vol = risk.btc_regime_series(panels.close[bench], cfg.risk, bpd, bpy)
    btc_mom = btc_mom.to_numpy(dtype="float64")
    btc_vol = btc_vol.to_numpy(dtype="float64")

    if randomize_score:
        wprof = rng.normal(size=wprof.size)

    # ---- storage -----------------------------------------------------------
    score_matrix = np.full((D, N), np.nan)
    mask_matrix = np.zeros((D, N), dtype=bool)
    fz_matrix = {k: np.full((D, N), np.nan) for k in FACTOR_NAMES}

    # ---- loop state --------------------------------------------------------
    held = np.zeros(N)
    base_pos = np.zeros(N)          # unit-gross book currently in force (vol estimator input)
    prev_long = np.zeros(0, dtype=int)
    prev_short = np.zeros(0, dtype=int)

    equity, peak = 1.0, 1.0
    base_ret_hist = np.zeros(T)
    vol_win = max(5, int(round(cfg.risk.vol_est_window_days * bpd)))
    last_reb_day = -1
    turnover_used_today = 0.0
    daily_budget = cfg.execution.max_daily_turnover
    dec2col = {int(d): i for i, d in enumerate(dec_idx)}

    cols = ["net_ret", "gross_ret", "long_ret", "short_ret", "fee", "spread", "impact",
            "funding", "turnover", "gross_exposure", "net_exposure", "beta_exposure",
            "n_long", "n_short", "regime_scale", "total_scale", "dd_scale", "drawdown",
            "max_participation", "n_stale"]
    acc = {c: np.zeros(T) for c in cols}
    rebalances: List[dict] = []
    n_stale_total = 0

    name_gross = np.zeros((D, N))
    name_fund = np.zeros((D, N))
    name_cost = np.zeros((D, N))
    name_turn = np.zeros((D, N))
    weight_matrix = np.zeros((D, N), dtype="float32")
    cur_ridx = -1

    for t in range(1, T):
        d = t - 1
        ridx = dec2col.get(int(d), -1)
        is_exec = ridx >= 0
        cost_total = 0.0
        rs = vs = ds = scale = 1.0

        if is_exec:
            eq_dollars = cfg.initial_capital * equity     # equity as of the decision bar
            mask, _rank = universe_mask(close_d[ridx], age_d[ridx], adv_d[ridx],
                                        cfg.universe, min_hist)
            if not disable_risk_overlays:
                mask = mask & risk.liquidation_ok(atr_d[ridx], cfg.risk)

            zs: Dict[str, np.ndarray] = {}
            for k in FACTOR_NAMES:
                x = raw_d[k][ridx].copy()
                x[~mask] = np.nan
                z = zscore(x, cfg.factors.winsor_method, cfg.factors.mad_k,
                           cfg.factors.pct_clip)
                zs[k] = z
                fz_matrix[k][ridx] = z
            mask_matrix[ridx] = mask

            sub = {k: zs[k] for k in factor_subset}
            # Neutralisation happens inside the traded subset, on this bar only --
            # including factors that the composite does not use would orthogonalise
            # against information the book never sees.
            if cfg.factors.neutralize and len(factor_subset) > 1:
                sub = residualize(sub, factor_subset,
                                  rescale=cfg.factors.neutralize_rescale)
            prof = np.asarray([wprof[FACTOR_NAMES.index(k)] for k in factor_subset])
            if selection_mode in ("random_score", "random_pick"):
                sc = rng.normal(size=N)
            else:
                sc = composite_score(sub, prof, factors=factor_subset)
            if score_permute == "cross_section":
                pool = np.flatnonzero(mask)
                if pool.size > 1:
                    sc[pool] = sc[pool][rng.permutation(pool.size)]
            sc[~mask] = np.nan
            score_matrix[ridx] = sc

            sel = select_book(sc, mask, cfg.portfolio, prev_long, prev_short)
            u_long, u_short = build_units(sc, vol_d[ridx], sel, cfg.portfolio)

            bu = side_beta(beta_d[ridx], sel.long_idx, u_long)
            bv = side_beta(beta_d[ridx], sel.short_idx, u_short)

            if disable_risk_overlays:
                g_long = g_short = 1.0
            else:
                g_long, g_short = side_gross_targets(bu, bv, 1.0, cfg.portfolio,
                                                     cfg.portfolio.beta_neutral_mode)

            base = np.zeros(N)
            if sel.long_idx.size:
                base[sel.long_idx] += u_long * g_long
            if sel.short_idx.size:
                base[sel.short_idx] -= u_short * g_short
            gsum = float(np.abs(base).sum())
            base_unit = base / gsum if gsum > 0 else base

            if disable_risk_overlays:
                target = base_unit
            else:
                rs = risk.regime_scale(btc_mom[d], cfg.risk)
                vs = risk.btc_vol_scale(btc_vol[d], cfg.risk)
                rv = float(np.std(base_ret_hist[max(0, d - vol_win):d], ddof=0) * np.sqrt(bpy))
                vts = risk.vol_target_scale(rv, cfg.risk)
                ds, _stop = risk.dd_scale(equity / peak - 1.0, cfg.risk,
                                          cfg.risk.dd_stop_new_entries_above)
                scale = float(np.clip(rs * vs * vts * ds, 0.0, cfg.risk.max_gross_exposure))
                target = base_unit * scale
            if extra_gross is not None:
                target = target * float(extra_gross[ridx])

            delta = target - held
            if daily_budget is not None and not disable_risk_overlays:
                day = int(d // bpd)
                if day != last_reb_day:
                    last_reb_day, turnover_used_today = day, 0.0
                delta = delta * risk.turnover_budget_scale(delta, turnover_used_today,
                                                           daily_budget)
            turnover_used_today += float(np.abs(delta).sum())

            delta, binding = risk.adv_cap_delta(delta, adv_d[ridx], eq_dollars, cfg.risk)
            held = held + delta

            if disable_costs:
                cost = {"total": np.zeros(N), "fee": np.zeros(N), "spread": np.zeros(N),
                        "impact": np.zeros(N), "participation": np.zeros(N)}
            else:
                cost = trade_cost(delta, adv_d[ridx], eq_dollars, daily_vol_d[ridx], cfg.costs)
            cost_total = float(cost["total"].sum())
            acc["fee"][t] = float(cost["fee"].sum())
            acc["spread"][t] = float(cost["spread"].sum())
            acc["impact"][t] = float(cost["impact"].sum())
            acc["turnover"][t] = float(np.abs(delta).sum())
            part = np.where(np.isfinite(cost["participation"]), cost["participation"], 0.0)
            acc["max_participation"][t] = float(part.max()) if part.size else 0.0
            acc["regime_scale"][t] = rs
            acc["total_scale"][t] = scale
            acc["dd_scale"][t] = ds
            name_cost[ridx] += cost["total"]
            name_turn[ridx] += np.abs(delta)
            weight_matrix[ridx] = held
            cur_ridx = ridx

            base_pos = base_unit
            prev_long, prev_short = sel.long_idx, sel.short_idx
            rebalances.append({
                "ts": ts[d], "exec_ts": ts[t], "n_universe": int(mask.sum()),
                "long": [insts[i] for i in sel.long_idx],
                "short": [insts[i] for i in sel.short_idx],
                "w_long": [float(target[i]) for i in sel.long_idx],
                "w_short": [float(target[i]) for i in sel.short_idx],
                "exposure": float(np.abs(target).sum()),
                "beta_u": bu, "beta_v": bv, "g_long": g_long, "g_short": g_short,
                "regime_scale": rs, "btc_vol_scale": vs, "dd_scale": ds, "scale": scale,
                "n_replaced": sel.n_replaced_long + sel.n_replaced_short,
                "binding_adv": binding,
                "long_gross": float(np.abs(target[sel.long_idx]).sum()),
                "short_gross": float(np.abs(target[sel.short_idx]).sum()),
            })

        # ---- stale-data guard ---------------------------------------------
        dead = ~alive[t]
        stale_mask = dead & (np.abs(held) > 0)
        n_stale = int(np.count_nonzero(stale_mask))
        if n_stale:
            j = np.flatnonzero(stale_mask)
            stale_notional = float(np.sum(np.abs(held[j])))
            stale_fee = stale_notional * cfg.costs.taker_fee
            stale_spread = stale_notional * (cfg.costs.half_spread_bps_illiquid / 1e4)
            cost_total += stale_fee + stale_spread
            # Book the force-close into the itemised columns as well.  If only
            # ``cost_total`` were raised, ``net_ret`` would contain a cost that
            # ``fee + spread + impact`` does not, so the cost decomposition would
            # silently understate trading cost on every forced exit.
            acc["fee"][t] += stale_fee
            acc["spread"][t] += stale_spread
            acc["turnover"][t] += stale_notional
            held[j] = 0.0
            base_pos[j] = 0.0
            n_stale_total += n_stale
        acc["n_stale"][t] = n_stale

        # ---- P&L ------------------------------------------------------------
        r = ret_exec[t]
        long_ret = float(np.sum(held * (held > 0) * r))
        short_ret = float(np.sum(held * (held < 0) * r))
        gross = long_ret + short_ret
        fr = np.where(np.isfinite(arr["funding"][t]), arr["funding"][t], 0.0)
        # ``fund`` is the funding CASHFLOW: -Σ w·rate, so a long on an instrument with
        # a positive rate already comes out negative (the long pays).  It therefore has
        # to be ADDED to the ledger, not subtracted -- subtracting it would double
        # negate and book every funding cost as a credit.
        fund = -cfg.costs.funding_multiplier * float(np.dot(held, fr))
        net = gross - cost_total + fund

        acc["gross_ret"][t] = gross
        acc["long_ret"][t] = long_ret
        acc["short_ret"][t] = short_ret
        acc["funding"][t] = fund
        acc["net_ret"][t] = net
        acc["gross_exposure"][t] = float(np.abs(held).sum())
        acc["net_exposure"][t] = float(held.sum())
        acc["beta_exposure"][t] = float(np.dot(held, np.nan_to_num(beta_arr[t], nan=0.0)))
        acc["n_long"][t] = int(np.count_nonzero(held > 0))
        acc["n_short"][t] = int(np.count_nonzero(held < 0))

        if cur_ridx >= 0:
            name_gross[cur_ridx] += held * r
            name_fund[cur_ridx] += (-cfg.costs.funding_multiplier) * held * fr

        equity *= (1.0 + net)
        peak = max(peak, equity)
        acc["drawdown"][t] = equity / peak - 1.0
        base_ret_hist[t] = float(np.dot(base_pos, r))

    bars = pd.DataFrame(acc, index=ts)
    bars["equity"] = (1.0 + bars["net_ret"]).cumprod()
    bars["drawdown"] = bars["equity"] / bars["equity"].cummax() - 1.0

    return BacktestResult(
        insts=insts, bars=bars, rebalances=rebalances, reb_ts=ts[dec_idx],
        score_matrix=score_matrix.astype("float32"), mask_matrix=mask_matrix,
        beta_matrix=beta_d.astype("float32"), adv_matrix=adv_d.astype("float32"),
        factor_zs={k: v.astype("float32") for k, v in fz_matrix.items()},
        fc_raw={k: v.astype("float32") for k, v in raw_d.items()},
        weight_matrix=weight_matrix,
        name_gross=name_gross.astype("float32"),
        name_cost=name_cost.astype("float32"),
        name_turnover=name_turn.astype("float32"),
        name_fund=name_fund.astype("float32"),
        cfg=cfg,
        meta={"n_rebalances": D, "n_stale_marks": n_stale_total, "warmup_bars": warmup,
              "factor_subset": list(factor_subset), "exec_price": cfg.execution.exec_price,
              "neutralize": bool(cfg.factors.neutralize), "has_name_fund": True},
    )
