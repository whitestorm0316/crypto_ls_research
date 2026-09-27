"""Top-K long/short book construction: selection, holding-stability, inverse-vol sizing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import warnings

import numpy as np

from ..config.settings import PortfolioConfig

EPS = 1e-12

# Cap/n combinations already reported as an infeasible cap, so a 700-rebalance
# backtest prints one warning instead of 1400.
_CAP_DEGRADED: set = set()


def reset_degradation_cache() -> None:
    """Tests only: forget which cap/n pairs have been reported."""
    _CAP_DEGRADED.clear()


@dataclass
class Selection:
    """Result of a name-selection pass at one rebalance."""
    long_idx: np.ndarray
    short_idx: np.ndarray
    n_replaced_long: int = 0
    n_replaced_short: int = 0
    n_dropped_by_score: int = 0
    diagnostics: Dict[str, float] = field(default_factory=dict)


def _candidates(score: np.ndarray, mask: np.ndarray, min_abs_score: float,
                side: str) -> np.ndarray:
    ok = mask & np.isfinite(score)
    if side == "long":
        ok &= score >= min_abs_score
        order = np.argsort(-np.where(ok, score, -np.inf), kind="stable")
    else:
        ok &= score <= -min_abs_score
        order = np.argsort(np.where(ok, score, np.inf), kind="stable")
    idx = order[ok[order]]
    return idx


def _stable_pick(ordered: np.ndarray, k: int, prev: np.ndarray,
                 hold_buffer: int, n_drop: int) -> Tuple[np.ndarray, int]:
    """Hold incumbents that are still competitive; cap the number of replacements.

    `n_drop` is the maximum number of *incumbent* names that may be dropped in one
    rebalance.  If more incumbents fall out of the buffer than the budget allows, the
    best-ranked incumbents are retained so the book still reaches K names -- turnover is
    throttled at the replacement level rather than by silently shrinking the book.
    """
    cand = [int(i) for i in ordered]
    cand_pos = {i: p for p, i in enumerate(cand)}
    if k <= 0:
        return np.zeros(0, dtype=int), 0
    if len(prev) == 0:
        return ordered[:k], 0

    prev_in = [int(i) for i in prev if int(i) in cand_pos]
    prev_set = set(prev_in)
    new_pool = [i for i in cand if i not in prev_set]

    keep = [i for i in prev_in if cand_pos[i] < k + hold_buffer]
    min_keep = max(0, k - max(0, int(n_drop)))
    if len(keep) < min_keep:
        extra = sorted((i for i in prev_in if i not in keep), key=lambda i: cand_pos[i])
        keep = keep + extra[: min_keep - len(keep)]
    keep = keep[:k]

    slots = k - len(keep)
    out = keep + new_pool[: max(0, slots)]
    replaced = len(prev) - len(keep)
    return np.asarray(out[:k], dtype=int), replaced


def select_book(score: np.ndarray, mask: np.ndarray, cfg: PortfolioConfig,
                prev_long: Optional[np.ndarray] = None,
                prev_short: Optional[np.ndarray] = None) -> Selection:
    prev_long = np.asarray(prev_long if prev_long is not None else [], dtype=int)
    prev_short = np.asarray(prev_short if prev_short is not None else [], dtype=int)
    k = cfg.top_k

    long_c = _candidates(score, mask, cfg.min_abs_score, "long")
    short_c = _candidates(score, mask, cfg.min_abs_score, "short")

    dropped = int(mask.sum() - np.isfinite(score[mask]).sum())

    li, rl = _stable_pick(long_c, k, prev_long, cfg.hold_rank_buffer, cfg.n_drop)
    si, rs = _stable_pick(short_c, k, prev_short, cfg.hold_rank_buffer, cfg.n_drop)
    return Selection(long_idx=li, short_idx=si, n_replaced_long=rl, n_replaced_short=rs,
                     n_dropped_by_score=dropped)


def inverse_vol_weights(score: np.ndarray, vol: np.ndarray, idx: np.ndarray,
                        cfg: PortfolioConfig) -> np.ndarray:
    """w_i  ∝  |score_i|^sp * (1/vol_i)^vp, normalised to sum to 1, capped and re-normalised.

    Returns a *unit* weight vector (sum == 1) or an empty array.  Any negative weight is
    impossible by construction (all terms are positive magnitudes).
    """
    if idx.size == 0:
        return np.zeros(0)
    s = np.abs(score[idx].astype("float64"))
    v = vol[idx].astype("float64")
    s = np.where(np.isfinite(s) & (s > 0), s, EPS)
    v = np.where(np.isfinite(v) & (v > 0), v, np.nan)
    if np.isnan(v).all():
        v = np.ones_like(v)
    else:
        v = np.where(np.isnan(v), np.nanmedian(v), v)

    raw = (s ** cfg.score_weight_power) * ((1.0 / v) ** cfg.vol_weight_power)
    raw = np.where(np.isfinite(raw) & (raw > 0), raw, EPS)

    cap = cfg.max_weight_per_instrument
    n = idx.size
    if cap is None or cap <= 0 or cap >= 1.0:
        return raw / raw.sum()

    # --- Width-aware cap -----------------------------------------------------
    # A *fixed* cap cannot bind at every pool width, and this pool's width is not
    # constant: the PIT universe ranges from 4 to 42 names, so the per-side book
    # ranges from 1 to 10.  With cap=0.20 every side of <= 5 names has
    # cap*n <= 1, where **equal weight is the only feasible allocation** -- there
    # is no other way to place >= 20% in each of 5 names.  So the sizing bug is
    # not fully "fixed" by raising the cap; it just moves to the narrow regimes.
    #
    # cap_width_slack > 0 relaxes the cap only as far as feasibility requires,
    #     cap_eff = max(cap, (1 + slack) / n),
    # so sizing keeps operating in narrow pools while no single name may exceed
    # (1 + slack) x equal weight.  slack = 0 reproduces the historical behaviour.
    slack = float(getattr(cfg, "cap_width_slack", 0.0) or 0.0)
    if slack > 0.0 and cap * n <= 1.0 + 1e-12:
        cap = min(1.0, max(cap, (1.0 + slack) / n))

    # Infeasible cap: K names cannot each be <= cap while summing to 1.
    # Degrade to the tightest feasible allocation (equal weight) rather than
    # silently returning an un-normalised vector.
    #
    # This used to be silent, and that mattered: the spec default was
    # top_k=10 with cap=0.10, i.e. cap*n == 1.0 exactly, so the *documented*
    # |score|/vol sizing never ran and every headline number was an equal-weight
    # backtest.  The fallback allocation is still the right one -- it is the
    # silence that was wrong.  Warn (once per cap/n pair) whenever the caller
    # asked for sizing the cap cannot accommodate.
    if cap * n <= 1.0 + 1e-12:
        if cfg.score_weight_power or cfg.vol_weight_power:
            key = (round(float(cap), 10), int(n))
            if key not in _CAP_DEGRADED:
                _CAP_DEGRADED.add(key)
                warnings.warn(
                    f"max_weight_per_instrument={cap} with n={n} selected names gives "
                    f"cap*n={cap * n:.3f} <= 1: the cap is infeasible, so scores and "
                    f"volatilities are IGNORED and the book is equal-weighted. Set "
                    f"max_weight_per_instrument > 1/n (e.g. 0.20 for K=10) to make "
                    f"|score|/vol sizing actually bind.",
                    UserWarning, stacklevel=3)
        return np.full(n, 1.0 / n)

    w = (raw / raw.sum()).astype("float64").copy()
    for _ in range(100):
        over = w > cap + 1e-15
        if not over.any():
            break
        w[over] = cap
        head = 1.0 - float(w[over].sum())
        free = ~over
        if head <= 0 or not free.any():
            break
        rest = w[free]
        s = float(rest.sum())
        if s <= 0:
            break
        w[free] = rest / s * head
    tot = float(w.sum())
    return w / tot if tot > 0 else np.full(n, 1.0 / n)


def build_units(score: np.ndarray, vol: np.ndarray, sel: Selection,
                cfg: PortfolioConfig) -> Tuple[np.ndarray, np.ndarray]:
    """Unit (sum-to-1) long and short magnitude weight vectors, plus index arrays."""
    return (inverse_vol_weights(score, vol, sel.long_idx, cfg),
            inverse_vol_weights(score, vol, sel.short_idx, cfg))


def rank_order(score: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Cross-sectional rank (1 = highest score) inside the PIT pool, NaN elsewhere."""
    out = np.full(score.shape, np.nan)
    idx = np.flatnonzero(mask & np.isfinite(score))
    if idx.size == 0:
        return out
    order = np.argsort(-score[idx], kind="stable")
    out[idx[order]] = np.arange(1, idx.size + 1)
    return out
