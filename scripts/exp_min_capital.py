"""Minimum viable capital for the v3 recommended config on OKX USDT-perps.

Question: "can I run this with $70?"

The binding constraint for a small account is NOT the strategy logic, it is the
*exchange minimum order size*.  OKX linear swaps are quoted in contracts (张):
    notional(USDT) = sz(contracts) * ctVal * price(USDT)
with sz constrained to `minSz + k*lotSz`.  So the smallest tradeable notional of
an instrument is `minSz * ctVal * price`, and a name whose weight is w needs
    capital >= min_notional / w
just to be representable at all.

Inputs
  data_cache/meta/swap_specs.json     (fetched here; also closes the known gap in
                                       instruments.parquet which lacked minSz/lotSz)
  artifacts/v3/baseline.pkl           realised per-name weights of the delivered config

Outputs (artifacts/min_capital/)
  min_capital_by_name.csv   per-instrument minimum capital, sorted
  capital_scenarios.csv     capital -> coverage of the pool / of realised names
  report.json               headline numbers
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from typing import Dict, List

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crypto_ls_research.data.okx_client import OKXClient  # noqa: E402

META = os.path.join(ROOT, "data_cache", "meta")
SPECS = os.path.join(META, "swap_specs.json")
OUT = os.path.join(ROOT, "artifacts", "min_capital")
os.makedirs(OUT, exist_ok=True)

KEEP = ["instId", "ctVal", "ctValCcy", "ctMult", "ctType", "lotSz", "minSz",
        "tickSz", "state", "settleCcy", "listTime", "uly"]


def fetch_specs(force: bool = False) -> pd.DataFrame:
    if os.path.exists(SPECS) and not force:
        rows = json.load(open(SPECS))
        print(f"[specs] cached: {len(rows)} rows from {os.path.basename(SPECS)}")
    else:
        cli = OKXClient()
        rows = cli.instruments("SWAP")
        json.dump(rows, open(SPECS, "w"))
        print(f"[specs] fetched {len(rows)} SWAP instruments -> {SPECS}")
    df = pd.DataFrame(rows)
    for c in ("ctVal", "ctMult", "lotSz", "minSz", "tickSz", "listTime"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    cols = [c for c in KEEP if c in df.columns]
    return df[cols]


def live_prices(insts: List[str]) -> pd.DataFrame:
    """Last price per instrument.  Prefer the local candle cache (free, works
    offline); fall back to OKX tickers for anything missing."""
    cache = os.path.join(ROOT, "data_cache", "candles", "1h")
    out = {}
    for i in insts:
        p = os.path.join(cache, f"{i}.parquet")
        if os.path.exists(p):
            try:
                c = pd.read_parquet(p, columns=["close"])
                if len(c):
                    out[i] = float(c["close"].iloc[-1])
            except Exception:                              # noqa: BLE001
                pass
    if len(out) < len(insts):
        missing = [i for i in insts if i not in out]
        print(f"[prices] cache missed {len(missing)}; fetching tickers")
        try:
            cli = OKXClient()
            tk = pd.DataFrame(cli.get("/api/v5/market/tickers", {"instType": "SWAP"}))
            tk["last"] = pd.to_numeric(tk["last"], errors="coerce")
            for _, r in tk.iterrows():
                if r["instId"] in missing and np.isfinite(r["last"]):
                    out[r["instId"]] = float(r["last"])
        except Exception as e:                             # noqa: BLE001
            print(f"[prices] ticker fetch failed: {type(e).__name__}: {e}")
    return pd.DataFrame({"instId": list(out), "px": list(out.values())})


def main() -> None:
    specs = fetch_specs()
    usdt = specs[(specs["settleCcy"] == "USDT") & (specs["state"].isin(["live", "suspend"]))]
    print(f"[specs] USDT linear swaps: {len(usdt)}  "
          f"minSz values: {sorted(usdt['minSz'].dropna().unique())[:12]}")

    # ---- realised weights of the delivered config --------------------------
    res = pickle.load(open(os.path.join(ROOT, "artifacts", "v3", "baseline.pkl"), "rb"))["result"]
    W = np.asarray(res.weight_matrix, dtype=float)          # (D_reb, N)
    insts = list(res.insts)
    print(f"[v3] weight_matrix {W.shape}, {len(insts)} instruments")

    nz = W[np.abs(W) > 1e-12]
    print(f"[v3] per-name |w|: median {np.median(np.abs(nz)):.4f}  "
          f"p10 {np.percentile(np.abs(nz), 10):.4f}  max {np.abs(nz).max():.4f}")

    gross = np.abs(W).sum(axis=1)
    nheld = (np.abs(W) > 1e-12).sum(axis=1)
    print(f"[v3] gross exposure median {np.median(gross):.4f}  "
          f"names held median {np.median(nheld):.0f}")

    # the names that are actually ever traded (ever non-zero)
    ever = np.abs(W).max(axis=0)
    traded = [insts[i] for i in np.where(ever > 1e-12)[0]]
    print(f"[v3] distinct instruments ever held: {len(traded)}")

    px = live_prices(traded)
    m = pd.DataFrame({"instId": traded}).merge(px, on="instId", how="left")
    m = m.merge(usdt[["instId", "ctVal", "ctValCcy", "minSz", "lotSz", "ctMult"]],
                on="instId", how="left")
    m = m.merge(pd.DataFrame({"instId": insts,
                              "max_abs_w": np.abs(W).max(axis=0),
                              "typ_w": np.array([np.abs(W[:, i]).mean() for i in range(len(insts))])}),
                on="instId", how="left")

    m["min_notional"] = m["minSz"] * m["ctVal"] * m["px"]
    m["lot_notional"] = m["lotSz"] * m["ctVal"] * m["px"]
    # typical per-name weight while held (mean over rebalances where non-zero)
    typ = []
    for i, ins in enumerate(insts):
        col = np.abs(W[:, i])
        col = col[col > 1e-12]
        typ.append(float(np.median(col)) if len(col) else np.nan)
    m = m.drop(columns=["typ_w"]).merge(
        pd.DataFrame({"instId": insts, "typ_w": typ}), on="instId", how="left")

    m["cap_needed_at_cap"] = m["min_notional"] / 0.20          # if it were a 20% position
    m["cap_needed_at_typ"] = m["min_notional"] / m["typ_w"]

    m = m.sort_values("min_notional").reset_index(drop=True)
    m.to_csv(os.path.join(OUT, "min_capital_by_name.csv"), index=False)

    print("\n=== smallest min-notional instruments in the v3 pool ===")
    print(m[["instId", "px", "ctVal", "minSz", "min_notional", "typ_w",
             "cap_needed_at_typ"]].head(15).to_string(index=False))
    print("\n=== largest min-notional (the blockers) ===")
    print(m[["instId", "px", "ctVal", "minSz", "min_notional", "typ_w",
             "cap_needed_at_typ"]].tail(10).to_string(index=False))

    # ---- capital scenarios -------------------------------------------------
    rows = []
    n_held_med = float(np.median(nheld))
    for cap_usd in [70, 200, 500, 1000, 2000, 3000, 5000, 10000, 25000, 50000, 100000]:
        # budget per slot if you spread capital over the median number of names
        budget_at_cap = cap_usd * 0.20                    # single-name cap
        budget_typ = cap_usd * float(np.median(np.abs(nz)))
        ok_cap = int((m["min_notional"] <= budget_at_cap).sum())
        ok_typ = int((m["min_notional"] <= budget_typ).sum())
        rows.append({
            "capital_usd": cap_usd,
            "single_name_cap_20pct_usd": round(budget_at_cap, 2),
            "typical_name_weight_usd": round(budget_typ, 2),
            "names_tradeable_at_cap": ok_cap,
            "names_tradeable_at_typ": ok_typ,
            "coverage_at_cap": round(ok_cap / len(m), 4),
            "coverage_at_typ": round(ok_typ / len(m), 4),
        })
    sc = pd.DataFrame(rows)
    sc.to_csv(os.path.join(OUT, "capital_scenarios.csv"), index=False)
    print("\n=== capital scenarios ===")
    print(sc.to_string(index=False))

    # ---- the headline: minimum capital to trade the full spec --------------
    # Requirement: every name you intend to hold must clear its own min notional.
    # A name's weight in the delivered config is typ_w (median while held), and the
    # portfolio holds ~n_held_med names, so capital >= min_notional / typ_w.
    need = m["cap_needed_at_typ"].dropna()
    head = {
        "median_per_name_weight": float(np.median(np.abs(nz))),
        "gross_exposure_median": float(np.median(gross)),
        "names_held_median": n_held_med,
        "n_instruments_ever_held": len(m),
        "min_notional_min": float(m["min_notional"].min()),
        "min_notional_median": float(m["min_notional"].median()),
        "min_notional_max": float(m["min_notional"].max()),
        "capital_for_median_name": float(need.median()),
        "capital_for_75pct_of_names": float(need.quantile(0.75)),
        "capital_for_90pct_of_names": float(need.quantile(0.90)),
        "capital_for_every_name": float(need.max()),
        "capital_at_70usd_tradeable_names": int(
            (m["min_notional"] <= 70 * float(np.median(np.abs(nz)))).sum()),
    }
    json.dump(head, open(os.path.join(OUT, "report.json"), "w"), indent=2)
    print("\n=== headline ===")
    for k, v in head.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
