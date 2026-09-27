"""Cross-sectional standardisation and composite scoring.

All functions operate on a **single cross-section** (`np.ndarray` of length n_insts
with NaN for names outside the PIT pool).  Because each call only ever sees the
current bar, cross-sectional statistics can never leak future information.
"""
from __future__ import annotations

from typing import Dict, Mapping, Sequence, Tuple

import numpy as np

from ..factors.engine import FACTOR_NAMES


def _finite(x: np.ndarray) -> np.ndarray:
    return x[np.isfinite(x)]


def winsorize(x: np.ndarray, method: str = "mad", mad_k: float = 3.0,
              pct_clip: float = 0.02) -> np.ndarray:
    """Winsorise a cross-section in place-safe fashion, preserving NaN positions."""
    out = x.astype("float64", copy=True)
    f = _finite(out)
    if f.size < 5:
        return out
    if method == "none":
        return out
    if method == "mad":
        med = np.median(f)
        mad = np.median(np.abs(f - med))
        if mad <= 0:
            return out
        lo, hi = med - mad_k * 1.4826 * mad, med + mad_k * 1.4826 * mad
    elif method == "percentile":
        lo, hi = np.quantile(f, pct_clip), np.quantile(f, 1 - pct_clip)
    else:
        raise ValueError(f"unknown winsorise method: {method}")
    mask = np.isfinite(out)
    out[mask] = np.clip(out[mask], lo, hi)
    return out


def zscore(x: np.ndarray, method: str = "mad", mad_k: float = 3.0,
           pct_clip: float = 0.02) -> np.ndarray:
    """Cross-sectional z-score after winsorisation (all stats from this bar only)."""
    w = winsorize(x, method=method, mad_k=mad_k, pct_clip=pct_clip)
    f = _finite(w)
    out = np.full_like(w, np.nan, dtype="float64")
    if f.size < 5:
        return out
    mu = f.mean()
    sd = f.std(ddof=0)
    if not np.isfinite(sd) or sd < 1e-12:
        return out
    mask = np.isfinite(w)
    out[mask] = (w[mask] - mu) / sd
    # re-winsorise the scores themselves: a single 20-sigma outlier must not
    # dominate Top-K selection.
    out[mask] = np.clip(out[mask], -4.0, 4.0)
    return out


def normalize_weights(profile: Sequence[float]) -> np.ndarray:
    w = np.asarray(profile, dtype="float64")
    s = np.abs(w).sum()
    if s <= 0:
        raise ValueError("factor weight profile must not be all zeros")
    return w / s


def composite_score(zs: Mapping[str, np.ndarray], profile: Sequence[float],
                    factors: Sequence[str] = FACTOR_NAMES) -> np.ndarray:
    """Weighted average of factor z-scores.

    The profile is normalised to sum to 1 so that `min_abs_score` is interpretable
    as "cross-sectional z units" and is comparable across weight profiles.
    """
    w = normalize_weights(profile)
    if len(w) != len(factors):
        raise ValueError("profile length must match factors")
    n = len(next(iter(zs.values())))
    acc = np.zeros(n, dtype="float64")
    cnt = np.zeros(n, dtype="float64")
    for wi, fname in zip(w, factors):
        z = zs[fname].astype("float64", copy=False)
        m = np.isfinite(z)
        acc[m] += wi * z[m]
        cnt[m] += wi
    out = np.where(cnt > 0, acc / np.where(cnt > 0, cnt, 1.0), np.nan)
    return out


def zscore_all(factors: Mapping[str, np.ndarray], method: str, mad_k: float,
               pct_clip: float) -> Dict[str, np.ndarray]:
    return {k: zscore(v, method=method, mad_k=mad_k, pct_clip=pct_clip)
            for k, v in factors.items()}


# ---------------------------------------------------------------------------
# factor neutralisation (research brief §38: "Factor Neutralization")
# ---------------------------------------------------------------------------
def residualize(zs: Mapping[str, np.ndarray], factors: Sequence[str],
                rescale: bool = True,
                min_obs_slack: int = 2) -> Dict[str, np.ndarray]:
    """Strip each factor of what the *other* factors in `factors` already explain.

    For every factor f we run one cross-sectional OLS on this single bar::

        z_f  =  a  +  Σ_{g ≠ f} b_g · z_g  +  ε_f

    and keep ε_f.  Regressing every factor on all the others (rather than
    Gram-Schmidt) makes the result independent of the order in which the factors
    are listed, which matters because that order is a free parameter otherwise.

    Causality: the regression uses one bar only, so nothing but information
    available at the decision bar can enter -- the same argument that makes
    ``zscore`` safe.

    Degenerate inputs return the input unchanged rather than a fabricated number:
    with fewer than `n_factors + 1 + min_obs_slack` names, or a singular design
    (perfectly collinear factors), there is no residual to estimate, so the
    neutralised cross-section is undefined and neutralising would silently
    substitute noise for signal.
    """
    fac = list(factors)
    if len(fac) < 2:
        return {k: zs[k] for k in fac}
    n = len(zs[fac[0]])
    need = len(fac) + 1 + min_obs_slack
    out: Dict[str, np.ndarray] = {}
    for f in fac:
        others = [g for g in fac if g != f]
        cols = [zs[g].astype("float64", copy=False) for g in others]
        y = zs[f].astype("float64", copy=False)
        ok = np.isfinite(y)
        for c in cols:
            ok &= np.isfinite(c)
        res = np.full(n, np.nan, dtype="float64")
        if int(ok.sum()) >= need:
            X = np.column_stack([np.ones(int(ok.sum()))] + [c[ok] for c in cols])
            try:
                beta, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
                res[ok] = y[ok] - X @ beta
            except np.linalg.LinAlgError:                     # singular design
                res[ok] = y[ok]
        else:
            res = y.copy()
        if rescale:
            fin = res[np.isfinite(res)]
            if fin.size >= 5:
                sd = float(fin.std(ddof=0))
                if np.isfinite(sd) and sd > 1e-12:
                    res = np.where(np.isfinite(res), (res - fin.mean()) / sd, np.nan)
        out[f] = res
    return out


def standardize_profile(profile: Sequence[float]) -> Tuple[float, ...]:
    return tuple(np.round(normalize_weights(profile), 6).tolist())
