"""Can the delivered config actually be *executed* at small capital?

`exp_min_capital.py` shows the physical floor.  This script measures the thing
that actually kills a small account: **order granularity**.

A perp order is `sz` contracts with `sz = minSz + k*lotSz`, so the notional you
can hold in one name is quantised to steps of `lotSz * ctVal * price`.  The
delivered config spreads capital over ~29 names with a median per-name weight of
0.32%, so at $70 a median position is worth ~$0.22 -- while the median minimum
order in the pool is $0.76.  Half the book is unrepresentable.

Nothing here is modelled: we take the *realised* weights of the backtest
(`weight_matrix`), the *real* contract specs from OKX (minSz/lotSz/ctVal), and the
*real* prices at each of the 688 rebalance stamps.  Reported per capital level:

  names_kept        how many of the intended names survive rounding
  signal_coverage   sum|w_kept| / sum|w_intended|   (how much of the book is real)
  w_err             median |what you hold - what the model wanted|, in weight units
  net_hedge_slip    |sum(w_actual)| - |sum(w_intended)|: the market-neutrality
                    the strategy relies on is destroyed by rounding
  turnover_infl     realised one-way turnover / intended turnover
"""
from __future__ import annotations

import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

META = os.path.join(ROOT, "data_cache", "meta")
OUT = os.path.join(ROOT, "artifacts", "min_capital")
os.makedirs(OUT, exist_ok=True)
CANDLES = os.path.join(ROOT, "data_cache", "candles", "1h")

CAPITALS = [70, 100, 200, 500, 1000, 2000, 3000, 5000, 10000, 25000, 50000, 100000]


def load_prices(insts, ts_index) -> pd.DataFrame:
    cols = {}
    miss = []
    for i in insts:
        p = os.path.join(CANDLES, f"{i}.parquet")
        if not os.path.exists(p):
            miss.append(i)
            continue
        c = pd.read_parquet(p, columns=["close"])["close"]
        cols[i] = c.reindex(ts_index, method="ffill")
    if miss:
        print(f"[px] {len(miss)} instruments without a 1h cache: {miss[:6]}")
    df = pd.DataFrame(cols)
    print(f"[px] price matrix {df.shape}, NaN frac {df.isna().mean().mean():.4f}")
    return df


def quantise(target_notional: np.ndarray, ctval_px: np.ndarray,
             minsz: np.ndarray, lotsz: np.ndarray) -> np.ndarray:
    """Return the notional you can actually hold, given the target notional.

    Rule (what a real order router does):
      raw_sz = |target| / (ctVal * px)          contracts wanted
      if raw_sz < minSz / 2 -> skip the name    (too small to bother)
      else sz = max(minSz, round(raw_sz / lotSz) * lotSz)
    """
    raw = np.abs(target_notional) / ctval_px
    with np.errstate(invalid="ignore", divide="ignore"):
        sz = np.round(raw / lotsz) * lotsz
    sz = np.where(raw < minsz / 2.0, 0.0, np.maximum(sz, minsz))
    sz = np.where(np.isfinite(sz), sz, 0.0)
    return np.sign(target_notional) * sz * ctval_px


def main() -> None:
    specs = pd.DataFrame(json.load(open(os.path.join(META, "swap_specs.json"))))
    for c in ("ctVal", "minSz", "lotSz"):
        specs[c] = pd.to_numeric(specs[c], errors="coerce")

    res = pickle.load(open(os.path.join(ROOT, "artifacts", "v3", "baseline.pkl"), "rb"))["result"]
    W = np.asarray(res.weight_matrix, dtype=float)
    insts = list(res.insts)
    reb = res.reb_ts

    px = load_prices(insts, reb).reindex(columns=insts)

    sp = specs.set_index("instId").reindex(insts)
    ctval = sp["ctVal"].to_numpy(dtype=float)
    minsz = sp["minSz"].to_numpy(dtype=float)
    lotsz = sp["lotSz"].to_numpy(dtype=float)
    ok_spec = np.isfinite(ctval) & np.isfinite(minsz) & np.isfinite(lotsz)
    print(f"[specs] {ok_spec.sum()}/{len(insts)} instruments matched to OKX specs")

    pxv = px.to_numpy(dtype=float)
    ctval_px = ctval * pxv                                   # USDT per contract
    tradable = ok_spec & np.isfinite(ctval_px) & (ctval_px > 0)
    print(f"[specs] tradeable cells {tradable.sum()}/{tradable.size}")

    rows = []
    detail_last = None
    for C in CAPITALS:
        tgt = C * W                                           # target notional
        # spec vectors are 1-D (per instrument); they broadcast over (D, N)
        act = quantise(tgt, ctval_px, minsz, lotsz)
        act = np.where(tradable, act, 0.0)

        wa = act / C
        wi = np.where(tradable, W, 0.0)                       # intended, restricted to tradeable

        keep = np.abs(wi) > 1e-12
        kept = (np.abs(wa) > 1e-12) & keep
        kept_per_row = kept.sum(axis=1)
        int_per_row = keep.sum(axis=1)

        cov = np.where(np.abs(wi).sum(1) > 0,
                       np.abs(wa).sum(1) / np.maximum(np.abs(wi).sum(1), 1e-12), np.nan)
        slip = np.abs(wa.sum(1)) - np.abs(wi.sum(1))          # extra directional exposure
        werr = np.abs(wa - wi)[kept] if kept.any() else np.array([np.nan])

        tgt_to = np.abs(np.diff(wa, axis=0, prepend=np.zeros((1, wa.shape[1])))).sum(1)
        int_to = np.abs(np.diff(wi, axis=0, prepend=np.zeros((1, wi.shape[1])))).sum(1)

        rows.append({
            "capital_usd": C,
            "names_intended_median": float(np.median(int_per_row)),
            "names_kept_median": float(np.median(kept_per_row)),
            "names_kept_p10": float(np.percentile(kept_per_row, 10)),
            "signal_coverage_median": float(np.nanmedian(cov)),
            "signal_coverage_p10": float(np.nanpercentile(cov, 10)),
            "weight_err_median": float(np.nanmedian(werr)),
            "net_hedge_slip_median": float(np.median(np.abs(slip))),
            "net_hedge_slip_p90": float(np.percentile(np.abs(slip), 90)),
            "net_exposure_intended_median": float(np.median(np.abs(wi.sum(1)))),
            "net_exposure_actual_median": float(np.median(np.abs(wa.sum(1)))),
            "turnover_ratio_median": float(np.median(tgt_to / np.maximum(int_to, 1e-12))),
        })

        if C == 70:
            last = -1
            keep_last = np.abs(W[last]) > 1e-12
            detail_last = pd.DataFrame({
                "instId": np.array(insts)[keep_last],
                "w_intended": W[last][keep_last],
                "target_usd": tgt[last][keep_last],
                "px": pxv[last][keep_last],
                "usd_per_contract": ctval_px[last][keep_last],
                "min_usd": (minsz * ctval_px)[last][keep_last],
                "contracts": act[last][keep_last] / np.where(ctval_px[last][keep_last] > 0,
                                                             ctval_px[last][keep_last], np.nan),
                "usd_actual": act[last][keep_last],
                "w_actual": wa[last][keep_last],
            }).sort_values("w_intended", key=np.abs, ascending=False)
            detail_last["intended_but_skipped"] = np.abs(detail_last["w_actual"]) < 1e-12

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT, "granularity_scenarios.csv"), index=False)
    print("\n=== granularity scenarios ===")
    print(df.to_string(index=False))

    if detail_last is not None:
        detail_last.to_csv(os.path.join(OUT, "granularity_last_rebalance_70usd.csv"), index=False)
        print(f"\n=== last rebalance ({reb[-1].date()}) at $70: "
              f"{len(detail_last)} intended names ===")
        print(detail_last.head(25).to_string(index=False))

    # smallest capital at which the book is essentially intact
    feasible = df[(df["signal_coverage_median"] >= 0.95) &
                  (df["net_hedge_slip_median"] <= 0.02)]
    head = {
        "capitals_tested": CAPITALS,
        "truthful_floor_usd": float(feasible["capital_usd"].min()) if len(feasible) else None,
        "at_70usd": df[df.capital_usd == 70].iloc[0].to_dict(),
    }
    json.dump(head, open(os.path.join(OUT, "granularity_report.json"), "w"), indent=2)
    print("\n=== acceptance: coverage>=95% and median hedge slip<=2% ===")
    print(json.dumps(head, indent=2, default=str))


if __name__ == "__main__":
    main()
