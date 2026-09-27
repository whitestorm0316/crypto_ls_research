"""Build the spliced OKX + Binance funding series.

Problem
-------
OKX's public funding-rate-history endpoint retains only ~3 months.  The backtest
window is 2021-01-01 .. present, so a pure-OKX funding series would leave 95% of
the sample with funding = 0 -- which silently flatters a market-neutral book,
because funding is one of its real costs.

Method
------
  1. Read the OKX funding cache (`data_cache/funding/`) -> the authoritative
     series for the recent window.
  2. For every OKX instrument, find the Binance USDT-perp counterpart and
     download funding from 2021-01-01 to the OKX retention boundary.
  3. Binance has mixed funding intervals (8h/4h/1h).  A settlement rate is a
     charge per interval, so Binance rates are SUMMED inside each 8h window
     aligned to 00/08/16 UTC.  That yields the economically equivalent 8h charge
     and puts both sources on one scale.
  4. Splice: Binance strictly before the first OKX settlement, OKX from there on.
  5. Instruments with no Binance counterpart borrow the cross-sectional median
     funding of matched instruments at each timestamp (clearly labelled, and the
     affected notional share is reported).
  6. On the OKX/Binance overlap, measure per-instrument correlation and mean
     level difference.  This is the proxy-error diagnostic for the whole study.

Output
------
  data_cache/funding_hyb/<INST>.parquet        spliced series (what the backtest reads)
  data_cache/meta/funding_source.json          coverage + proxy-error diagnostics
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .binance_client import BinanceClient, okx_base
from .download import CACHE, ensure_dirs
from .okx_client import OKXClient

OUTDIR = os.path.join(CACHE, "funding_hyb")
EIGHT_H = 8 * 3600 * 1000


def _bucket_8h(ts: np.ndarray) -> np.ndarray:
    return ts - (ts % EIGHT_H)


def load_okx_funding() -> Dict[str, pd.Series]:
    src = os.path.join(CACHE, "funding")
    out: Dict[str, pd.Series] = {}
    if not os.path.isdir(src):
        return out
    for f in sorted(os.listdir(src)):
        if not f.endswith(".parquet"):
            continue
        inst = f[:-8]
        try:
            df = pd.read_parquet(os.path.join(src, f))
        except Exception:                                       # noqa: BLE001
            continue
        if len(df):
            out[inst] = df["fundingRate"].astype("float64").sort_index()
    return out


def to_8h_sum(times: List[int], rates: List[float]) -> pd.Series:
    """Binance settlements -> 8h charge, indexed by UTC 00/08/16."""
    if not times:
        return pd.Series(dtype="float64")
    ts = np.asarray(times, dtype="int64")
    r = np.asarray(rates, dtype="float64")
    b = _bucket_8h(ts)
    df = pd.DataFrame({"b": b, "r": r})
    g = df.groupby("b")["r"].sum()
    idx = pd.to_datetime(g.index.to_numpy(), unit="ms", utc=True)
    return pd.Series(g.to_numpy(), index=idx, dtype="float64").sort_index()


def _load_existing(outdir: str = OUTDIR) -> Dict[str, pd.Series]:
    """Read the spliced series already on disk (resume / medians reference)."""
    out: Dict[str, pd.Series] = {}
    if not os.path.isdir(outdir):
        return out
    for f in sorted(os.listdir(outdir)):
        if not f.endswith(".parquet"):
            continue
        try:
            out[f[:-len(".parquet")]] = pd.read_parquet(os.path.join(outdir, f))["fundingRate"]
        except Exception:                                        # noqa: BLE001
            continue
    return out


def build(start: str = "2021-01-01", end: str = "2026-09-26",
          rate: float = 8.0, workers: int = 8, verbose: bool = True,
          retries: int = 12, allow_proxy_fallback: bool = True,
          only_proxy: bool = False) -> dict:
    ensure_dirs()
    os.makedirs(OUTDIR, exist_ok=True)

    okx = load_okx_funding()
    if not okx:
        raise RuntimeError("no OKX funding cache found; run data.download first")

    # ---- resume mode: retry only what is currently a median proxy ------------
    # A rate-limit outage degrades instruments to the proxy silently; re-running the
    # whole build to fix that is wasteful, and skipping the fix is worse.  `only_proxy`
    # re-fetches just those instruments and keeps every already-real series intact.
    prev_series: Dict[str, pd.Series] = _load_existing() if only_proxy else {}
    if only_proxy:
        cov_path = os.path.join(CACHE, "meta", "funding_coverage.csv")
        if os.path.exists(cov_path):
            cov = pd.read_csv(cov_path)
            todo = set(cov.loc[cov["source"] == "okx+median_proxy", "inst"])
        else:
            todo = set(prev_series)
        if not todo:
            print("  [only-proxy] nothing is on the median proxy -> nothing to retry",
                  flush=True)
            return {"only_proxy": True, "retried": 0, "note": "nothing on the proxy"}
        okx = {i: s for i, s in okx.items() if i in todo}
        n_real = len(prev_series) - len(todo)
        print(f"  [only-proxy] retrying {len(okx)} proxied instrument(s); "
              f"{n_real} already-real series kept as-is", flush=True)

    # Each instrument is spliced at ITS OWN first OKX settlement, so the boundary
    # adapts to per-instrument retention.  The global reference (the point after
    # which every instrument has OKX data) is reported only.
    okx_ref = max(s.index[0] for s in okx.values())
    if verbose:
        print(f"OKX funding: {len(okx)} instruments, earliest settlement {okx_ref}")

    cli = BinanceClient(rate_per_sec=rate, max_workers=workers)
    live = cli.perp_symbols()
    if verbose:
        print(f"Binance live USDT perps: {len(live)}")

    mapping: Dict[str, Optional[str]] = {i: cli.match_symbol(okx_base(i), live) for i in okx}
    matched = {i: s for i, s in mapping.items() if s}
    if verbose:
        print(f"matched to Binance: {len(matched)}/{len(okx)}")

    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)

    def fetch(sym: str):
        return cli.funding_history(sym, start_ms, end_ms, retries=retries)

    if verbose:
        print("downloading Binance funding ...", flush=True)
    hist = cli.pmap(fetch, sorted(set(matched.values())), desc="binance-funding")

    # ---- fetch failure is NOT the same as "no such symbol" -------------------
    # ``pmap`` returns the string "ERR:<Exc>" for a failed request, and a rate-limit
    # outage therefore used to look exactly like "Binance has no history for this
    # symbol" -- which silently demoted the instrument to the median proxy.  A
    # proxy is a modelled number; an outage is a missing measurement.  They must
    # never be confused, so failures are counted, named, and (by default) loud.
    failed_syms = sorted(s for s, v in hist.items()
                         if isinstance(v, str) and v.startswith("ERR:"))
    if verbose and failed_syms:
        print(f"  !! {len(failed_syms)} Binance funding request(s) FAILED "
              f"(rate limit / network) -- those instruments would fall back to the "
              f"median proxy.  Examples: {failed_syms[:5]}", flush=True)
    if failed_syms and not allow_proxy_fallback:
        raise RuntimeError(
            f"{len(failed_syms)} Binance funding requests failed; refusing to fall back "
            f"to the median proxy with allow_proxy_fallback=False.  Re-run "
            f"`python -m crypto_ls_research.data.funding_build --only-proxy` to retry "
            f"just the affected instruments.")

    # ---- per-instrument spliced series + overlap diagnostics ----------------
    series: Dict[str, pd.Series] = {}
    diag_rows: List[dict] = []
    # ``real_splice`` = instruments that actually received Binance history.  This is
    # the ONLY honest test for "okx+binance": ``inst in matched`` merely means a
    # Binance *symbol* was found, not that its history was fetched.  Conflating the
    # two mislabels every rate-limited instrument as spliced.
    real_splice: set = set()
    for inst, sym in matched.items():
        rows = hist.get(sym)
        if not isinstance(rows, list) or not rows:
            continue
        bn = to_8h_sum([t for t, _ in rows], [r for _, r in rows])
        if not len(bn):
            continue
        o = okx.get(inst)
        # ---- proxy diagnostic on the overlap ----
        if o is not None:
            ov = bn.index.intersection(o.index)
            if len(ov) >= 10:
                a = bn.reindex(ov).to_numpy()
                b = o.reindex(ov).to_numpy()
                if np.std(a) > 0 and np.std(b) > 0:
                    diag_rows.append({
                        "inst": inst, "binance_symbol": sym, "n_overlap": int(len(ov)),
                        "corr": float(np.corrcoef(a, b)[0, 1]),
                        "mean_binance": float(np.mean(a)), "mean_okx": float(np.mean(b)),
                        "mean_diff": float(np.mean(a - b)),
                    })
            boundary = o.index[0]
            series[inst] = pd.concat([bn[bn.index < boundary], o]).sort_index()
        else:
            series[inst] = bn
        real_splice.add(inst)

    # ---- no Binance history -> cross-sectional median over the pre-OKX period ----
    unmatched = [i for i in okx if i not in real_splice]
    # In resume mode the median is still built from EVERY known series, otherwise the
    # reference for the retried names would itself be computed off a shrunken panel.
    ref_series = {**prev_series, **series}
    if ref_series:
        med = pd.concat({k: v for k, v in ref_series.items()}, axis=1).median(axis=1)
        for inst in unmatched:
            o = okx.get(inst)
            if o is None or not len(o):
                continue
            boundary = o.index[0]
            series[inst] = pd.concat([med[med.index < boundary], o]).sort_index()

    # ---- write -------------------------------------------------------------
    for inst, s in series.items():
        s = s[~s.index.duplicated(keep="last")].dropna()
        s.rename("fundingRate").to_frame().to_parquet(
            os.path.join(OUTDIR, f"{inst}.parquet"), compression="zstd")

    diag = pd.DataFrame(diag_rows)
    # Coverage always describes EVERY series on disk, not just the ones touched by
    # this run -- otherwise a resume run would shrink the coverage report and hide
    # the instruments it did not re-fetch.
    all_series = {**prev_series, **series} if only_proxy else dict(series)
    prev_src: Dict[str, str] = {}
    if only_proxy:
        cov_path = os.path.join(CACHE, "meta", "funding_coverage.csv")
        if os.path.exists(cov_path):
            _c = pd.read_csv(cov_path)
            prev_src = dict(zip(_c["inst"], _c["source"]))
    cov_rows = []
    for inst, s in all_series.items():
        if inst in series:
            src = ("okx+binance" if inst in real_splice
                   else "okx+median_proxy" if okx.get(inst) is not None
                   else "binance")
        else:
            src = prev_src.get(inst, "unknown")
        cov_rows.append({
            "inst": inst,
            "n": int(len(s)),
            "first": str(s.index[0].date()),
            "last": str(s.index[-1].date()),
            "source": src,
            "mean_rate": float(s.mean()),
            "ann_rate_pct": float(s.mean() * 3 * 365 * 100),
        })
    cov = pd.DataFrame(cov_rows)
    n_proxy = int((cov["source"] == "okx+median_proxy").sum())

    summary = {
        "okx_instruments": len(okx),
        "binance_symbol_found": len(matched),
        "binance_history_fetched": int(len(real_splice)),
        "unmatched": unmatched,
        "n_unmatched": int(len(unmatched)),
        "okx_retention_start": str(okx_ref),
        "spliced_instruments": int(len(all_series)),
        "proxy_instruments": n_proxy,
        "binance_requests": cli.stats["req"],
        "binance_errors": cli.stats["err"],
        "binance_failed_requests": len(failed_syms),
        "binance_failed_symbols": failed_syms[:50],
        "only_proxy_resume": bool(only_proxy),
        "caveat": ("instruments in 'unmatched' carry a CROSS-SECTIONAL MEDIAN funding "
                   "rate before their first OKX settlement; their funding P&L is "
                   "therefore partly synthetic.  Price P&L is unaffected."),
        "overlap_proxy_error": {
            "n_instruments": int(len(diag)),
            "corr_mean": float(diag["corr"].mean()) if len(diag) else None,
            "corr_median": float(diag["corr"].median()) if len(diag) else None,
            "corr_p10": float(diag["corr"].quantile(0.10)) if len(diag) else None,
            "mean_diff_bps": float(diag["mean_diff"].mean() * 1e4) if len(diag) else None,
        },
    }
    meta = os.path.join(CACHE, "meta")
    os.makedirs(meta, exist_ok=True)
    with open(os.path.join(meta, "funding_source.json"), "w") as f:
        json.dump(summary, f, indent=1)
    if len(diag):
        diag.to_csv(os.path.join(meta, "funding_proxy_diag.csv"), index=False)
    cov.to_csv(os.path.join(meta, "funding_coverage.csv"), index=False)

    if verbose:
        print(f"\nwrote {len(series)} spliced series to {OUTDIR}")
        print("source mix:", cov["source"].value_counts().to_dict())
        print("proxy error on overlap:", json.dumps(summary["overlap_proxy_error"], indent=1))
        print(f"median annualised funding (short-receive positive): "
              f"{cov['ann_rate_pct'].median():.2f}%/yr")
    return summary


if __name__ == "__main__":
    import argparse

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="build the spliced OKX+Binance funding series")
    ap.add_argument("--rate", type=float, default=8.0, help="Binance requests/second")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--retries", type=int, default=12, help="retries per request page")
    ap.add_argument("--only-proxy", action="store_true",
                    help="re-fetch ONLY the instruments currently on the median proxy")
    ap.add_argument("--no-proxy-fallback", action="store_true",
                    help="fail instead of substituting the cross-sectional median")
    a = ap.parse_args()
    build(rate=a.rate, workers=a.workers, retries=a.retries,
          allow_proxy_fallback=not a.no_proxy_fallback, only_proxy=a.only_proxy)
