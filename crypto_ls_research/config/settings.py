"""Global configuration for the Crypto Futures Liquidity-Flow Trend Market-Neutral strategy.

Design rule: EVERY time-window is expressed in DAYS and converted to bars using
`bars_per_day` for the active frequency.  This guarantees that switching 15m -> 1h -> 4h
keeps the economic horizon constant, so results across frequencies are comparable.

Nothing in this file may contain forward-looking information.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple

# ----------------------------------------------------------------------------
# data cache root
# ----------------------------------------------------------------------------
# The cache is a *variable*, not a constant, so the same pipeline can be pointed at
# a different venue's bars without a second code path: `CRYPTO_CACHE_DIR=... python -m
# crypto_ls_research.run.research ...`.  It must stay a single definition -- the
# downloader and the loader disagreeing about which cache they mean is exactly the
# class of silent bug this project keeps guarding against (cf. the `rebalance_days`
# literal that was duplicated in five places).
#
# The default is byte-for-byte the historical path, so an unset env var changes nothing.
CACHE_DIR: str = os.environ.get("CRYPTO_CACHE_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data_cache")
)

# ----------------------------------------------------------------------------
# frequency table
# ----------------------------------------------------------------------------
BARS_PER_DAY: Dict[str, int] = {
    "5m": 288,
    "15m": 96,
    "1h": 24,
    "4h": 6,
    "12h": 2,
    "1d": 1,
}

SECONDS_PER_BAR: Dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "12h": 43200,
    "1d": 86400,
}


# ----------------------------------------------------------------------------
# the accepted configuration
# ----------------------------------------------------------------------------
#: The rebalance grid of the **accepted** configuration -- the one the unattended
#: daemon trades, and therefore the one the acceptance headline has to describe.
#:
#: It lives here, in the config layer, because *three* places need the number and
#: none of them may own a copy of the others':
#:
#:   * `execution.engine.DEFAULT_SIGNAL` -- the strategy the desk plans on, and
#:     the argparse default of `auto_trader` / `run_live` / `auto_ctl`,
#:   * `webapp.spec.OPTIMAL_CLI` -- what the console calls "已验收最优",
#:   * the acceptance run itself (`artifacts/<OPTIMAL_TAG>/`).
#:
#: A second literal is not a hypothetical.  `DEFAULT_SIGNAL` said 3.0 while the
#: daemon was started with `--rebalance-days 1`; nothing compared them, so the
#: desk kept generating **3-day** plans and printing 「下次调仓」 three days out
#: while the daemon traded every day.  Nothing errored -- the plan was simply a
#: plan for a different strategy than the one running.
#:
#: History: the basis was 3 days until 2026-09-29, when it moved to 1 day so the
#: acceptance describes the configuration actually running.  Both runs are on
#: disk (`artifacts/v3` = 3 days, `artifacts/v4_1d` = 1 day) and **both numbers
#: must be quoted together** -- see `webapp/spec.py::OPTIMAL_HEADLINE` and
#: `artifacts/FINDINGS.md` Q22.  Moving this value is a re-designation of the
#: acceptance basis, not a parameter tweak.
ACCEPTED_REBALANCE_DAYS: float = 1.0

#: The **factor set** of the accepted configuration, for the same reason as the
#: grid above and read by the same three surfaces:
#:
#:   * `execution.engine.DEFAULT_SIGNAL["overrides"]["factors.subset"]` -- the
#:     strategy the desk plans on and the daemon trades,
#:   * `webapp.spec.OPTIMAL_OVERRIDES["factors.subset"]` -- what the console
#:     calls 「已验收最优」,
#:   * the acceptance run itself, recorded in
#:     `artifacts/<OPTIMAL_TAG>/tables/00_run_meta.json` (`factor_subset`).
#:
#: Note this is **not** `FactorConfig.subset`.  That one is the *schema* default
#: (the research brief's four factors, with `rev_short` deliberately absent so
#: every pre-existing number stays bit-identical); this one is the *accepted
#: model choice*, which is a different question and has been a different value
#: at every stage of the project's history.  Conflating the two is how a page
#: ends up describing a book nobody measured.
#:
#: History: pruned to `range_pos + hitrate` while the brief's four factors were
#: the default; widened to all five on 2026-09-29 after the acceptance run in
#: `artifacts/v5_1d_all5/` cleared 16/16 gates -- including the three placebo
#: nulls, which no earlier tag had ever produced.  `artifacts/v4_1d/` stays on
#: disk as the two-factor record and `artifacts/v3/` as the 3-day one; moving
#: this value is a re-designation of the acceptance basis, not a parameter tweak.
#: See `artifacts/FINDINGS.md` Q32/Q33 and `tests/test_accepted_factor_set.py`.
ACCEPTED_FACTORS: Tuple[str, ...] = ("momentum", "flow", "range_pos", "hitrate",
                                     "rev_short")


# ----------------------------------------------------------------------------
# execution / cost model
# ----------------------------------------------------------------------------
@dataclass
class CostConfig:
    """Explicit, itemised cost model.  No magic 'round trip' number anywhere."""

    maker_fee: float = 0.0002        # 0.02%  (limit / passive fill)
    taker_fee: float = 0.0005        # 0.05%  (market / aggressive fill)
    passive_fill_ratio: float = 0.0  # 0.0 -> all fills are taker (conservative default)

    # half-spread paid on each fill, in bps of notional (liquidity-tier proxy)
    half_spread_bps_base: float = 0.6      # top-tier perp
    half_spread_bps_illiquid: float = 3.0  # thin perp
    # liquidity tier is decided from *trailing* ADV, never full-sample ADV
    half_spread_adv_cut_usd: float = 25_000_000.0   # 30d ADV threshold

    # square-root market-impact law:  impact = impact_coef * daily_vol * sqrt(participation)
    impact_coef: float = 0.6
    # extra flat slippage for the signal->execution delay is NOT added here:
    # it is captured endogenously by marking PnL from the actual execution price.

    funding_multiplier: float = 1.0   # 0 = free funding, 1 = real, 1.5 / 2.0 stress
    fee_multiplier: float = 1.0      # fee stress


# ----------------------------------------------------------------------------
# universe
# ----------------------------------------------------------------------------
@dataclass
class UniverseConfig:
    min_history_days: float = 30.0    # listing-age gate (>= 30d => liq window is computable)
    liq_window_days: float = 30.0     # avg_amount lookback
    min_avg_amount_usd: float = 3_000_000.0   # 30d ADV floor (USDT)
    pit_topn: int = 100               # PIT liquidity rank cut
    min_price: float = 1e-8           # guard against degenerate/bad ticks


# ----------------------------------------------------------------------------
# factors
# ----------------------------------------------------------------------------
@dataclass
class FactorConfig:
    mom_lookback_days: float = 5.25      # 504 bars @15m
    mom_vol_days: float = 2.0            # 192 bars @15m
    flow_short_days: float = 1.0         # 96 bars @15m
    flow_long_days: float = 30.0         # 2880 bars @15m
    range_days: float = 7.5              # 720 bars @15m
    hitrate_days: float = 5.25           # 504 bars @15m
    # short-horizon reversal: minus the risk-adjusted return over this window.
    # The IC study shows every trend factor flips sign at the ~4h horizon, so this
    # factor states the reversal effect explicitly instead of leaving it as noise.
    rev_short_days: float = 1.0          # 24 bars @1h

    # composite weights: (momentum, flow, range, hitrate, rev_short) -> normalised
    # internally.  NOTE: `rev_short` is deliberately absent from the default
    # `subset` below, so every number produced with the spec-default 4-factor book
    # is bit-identical to before this factor existed.
    profiles: Dict[str, Tuple[float, ...]] = field(
        default_factory=lambda: {
            "A_MOM_TILT": (1.00, 0.40, 0.30, 0.20, 0.20),
            "B": (1.00, 0.50, 0.25, 0.25, 0.20),
            "C": (1.00, 0.33, 0.33, 0.33, 0.20),
            "D_EQUAL": (0.20, 0.20, 0.20, 0.20, 0.20),
        }
    )
    default_profile: str = "A_MOM_TILT"

    # cross-sectional normalisation
    winsor_method: str = "mad"        # "mad" | "percentile" | "none"
    mad_k: float = 3.0
    pct_clip: float = 0.02            # 2% / 98% clip when winsor_method == percentile

    # Which factors enter the composite score, and in what order.  The order must
    # match the corresponding entries of the active `profiles` tuple, because the
    # profile weights are indexed by factor name -- so a subset keeps its relative
    # weights and is re-normalised internally.
    #
    # Default = the spec's four factors.  Narrowing this is a *model* choice, not a
    # parameter: it must be justified out-of-sample (see run --stages wf / mc).
    subset: Tuple[str, ...] = ("momentum", "flow", "range_pos", "hitrate")

    # ---- factor neutralisation (research brief §38, previously unimplemented) ----
    # "Factor Neutralization" between the IC study and the composite score: each
    # factor keeps only the part that is orthogonal to the other factors in the
    # subset.  momentum <-> range_pos correlate 0.71, so without this the composite
    # double-counts one direction and dilutes the rest.
    neutralize: bool = False
    # Re-standardise the residual to unit cross-sectional std so the profile weights
    # keep the same meaning as in the un-neutralised case.
    neutralize_rescale: bool = True


# ----------------------------------------------------------------------------
# portfolio construction
# ----------------------------------------------------------------------------
@dataclass
class PortfolioConfig:
    top_k: int = 10
    min_abs_score: float = 0.0
    vol_weight_power: float = 1.0     # weight ~ |score|^1 * (1/vol)^1
    score_weight_power: float = 1.0
    max_weight_per_instrument: float = 0.10   # 10% of gross per name
    # Width-aware cap.  A static cap cannot bind at every pool width: with cap=0.20
    # any side of <=5 names has cap*n <= 1, where equal weight is the *only* feasible
    # allocation.  slack > 0 relaxes the cap to max(cap, (1+slack)/n) so |score|/vol
    # sizing keeps operating in narrow pools; no name may exceed (1+slack) x equal
    # weight.  0.0 = historical behaviour (and the loud warning below).
    cap_width_slack: float = 0.0
    beta_lookback_days: float = 7.0
    beta_neutral_mode: str = "B_beta_neutral"  # "A_gross_matched" | "B_beta_neutral"
    beta_ratio_cap: Tuple[float, float] = (0.5, 2.0)   # cap on short_gross/long_gross
    # portfolio stability
    hold_rank_buffer: int = 0         # keep a name unless it leaves top (K + buffer)
    n_drop: int = 999                 # max names replaced per rebalance


# ----------------------------------------------------------------------------
# risk overlays
# ----------------------------------------------------------------------------
@dataclass
class RiskConfig:
    target_vol_annual: float = 0.30
    min_gross_exposure: float = 0.30
    max_gross_exposure: float = 2.00
    vol_est_window_days: float = 10.0

    regime_enabled: bool = True
    regime_mom_days: float = 20.0
    regime_bear_scale: float = 0.55

    btc_vol_cap_enabled: bool = True
    btc_vol_window_days: float = 10.0
    btc_vol_soft: float = 0.60        # annualised; above this start de-risking
    btc_vol_hard: float = 1.20        # annualised; at this level scale = min_scale
    btc_vol_min_scale: float = 0.35

    # (drawdown threshold, multiplier); floor is deliberately > 0 to avoid the
    # classic "locked permanently flat at the trough" failure mode.
    dd_ladder: Tuple[Tuple[float, float], ...] = (
        (0.10, 1.00),
        (0.15, 0.75),
        (0.20, 0.50),
        (0.30, 0.25),
    )
    dd_stop_new_entries_above: float = 0.20

    max_adv_participation: float = 0.05       # per rebalance, per instrument
    max_leverage: float = 5.0                 # account leverage -> liquidation distance
    min_liquidation_atr_multiple: float = 3.0
    atr_days: float = 1.0


# ----------------------------------------------------------------------------
# execution
# ----------------------------------------------------------------------------
@dataclass
class ExecutionConfig:
    exec_price: str = "next_open"     # next_open | next_vwap | next_twap | next_close
    max_daily_turnover: float = 0.50  # 50% of gross per day; None = unlimited
    turnover_scale_mode: str = "proportional"


# ----------------------------------------------------------------------------
# backtest
# ----------------------------------------------------------------------------
@dataclass
class BacktestConfig:
    bar: str = "15m"
    start: str = "2021-01-01"
    end: str = "2026-09-26"
    rebalance_bars: int = 96          # = 1 day @15m by default
    initial_capital: float = 100_000.0
    benchmark_inst: str = "BTC-USDT-SWAP"

    universe: UniverseConfig = field(default_factory=UniverseConfig)
    factors: FactorConfig = field(default_factory=FactorConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    costs: CostConfig = field(default_factory=CostConfig)

    # ---- convenience -------------------------------------------------------
    @property
    def bars_per_day(self) -> int:
        return BARS_PER_DAY[self.bar]

    @property
    def bars_per_year(self) -> float:
        return 365.0 * self.bars_per_day

    def days_to_bars(self, days: float) -> int:
        return max(1, int(round(days * self.bars_per_day)))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["_bars_per_day"] = self.bars_per_day
        return d


def default_config(bar: str = "15m", rebalance_bars: int = 96, **overrides) -> BacktestConfig:
    cfg = BacktestConfig(bar=bar, rebalance_bars=rebalance_bars)
    for k, v in overrides.items():
        if not hasattr(cfg, k):
            raise KeyError(f"unknown BacktestConfig field: {k}")
        setattr(cfg, k, v)
    return cfg


REBALANCE_PRESETS: Dict[str, int] = {
    "15m": 1,
    "1h": 4,
    "4h": 16,
    "12h": 48,
    "1d": 96,
}
