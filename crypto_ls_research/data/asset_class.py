"""Instrument **asset class** classification, and the pool scoping that depends on it.

Why this exists
---------------
The OKX USDT-perp universe is *not* a pure crypto universe: alongside crypto
derivatives it lists tokenised equities/ETFs (NVDA, TSLA, QQQ, SOXL, ...) and
commodities (XAU gold, XAG silver, CL WTI, BZ Brent).  Trading them inside a
"crypto liquidity-flow / trend" strategy silently changes what the research
claims to be about, so the scope has to be an explicit, auditable choice rather
than an accident of whatever the exchange happened to list.

Source of truth
---------------
OKX's own `/api/v5/public/instruments` response carries `instCategory`:

    1 -> crypto derivative         (in scope)
    3 -> tokenised equity / ETF    (out of scope)
    4 -> commodity                  (out of scope)

We freeze that mapping into `data_cache/meta/inst_category.json` so a run is
reproducible even if the exchange reclassifies a contract later.  We never
*guess* the class from a hand-written ticker list -- an eyeballed list is how
this project previously mis-stated the count (see the note in the report).

Fail-loud policy: if a contract in the cache has no entry in the frozen
snapshot we raise instead of silently dropping it.  A silent drop would shrink
the tradable pool, which is exactly the kind of quiet universe change that makes
two backtests incomparable.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence, Tuple

from .store import CACHE

CATEGORY_FILE = os.path.join(CACHE, "meta", "inst_category.json")

CRYPTO_CATEGORY = "1"
CATEGORY_LABELS: Dict[str, str] = {
    "1": "crypto",
    "2": "index",
    "3": "equity",
    "4": "commodity",
    "5": "forex",
}

ASSET_CLASSES = ("all", "crypto")


def build_category_snapshot(path: str = CATEGORY_FILE, proxy: Optional[str] = None) -> Dict:
    """Query OKX and freeze the instrument -> instCategory map to `path`."""
    from .okx_client import OKXClient

    cli = OKXClient(**({"proxy": proxy} if proxy else {}))
    rows = cli.instruments("SWAP")
    cats: Dict[str, str] = {}
    for r in rows:
        if r.get("settleCcy") != "USDT":
            continue
        cat = str(r.get("instCategory", "") or "")
        cats[r["instId"]] = cat or "unknown"
    payload = {
        "source": "OKX /api/v5/public/instruments?instType=SWAP",
        "field": "instCategory",
        "labels": CATEGORY_LABELS,
        "n_instruments": len(cats),
        "categories": cats,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
    return payload


def load_categories(path: str = CATEGORY_FILE) -> Dict[str, str]:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found -- freeze the classification first:\n"
            f"  python -m crypto_ls_research.data.asset_class --build"
        )
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d["categories"]


def classify(inst: str) -> str:
    return CATEGORY_LABELS.get(inst, f"category_{inst}")


def filter_insts(insts: Sequence[str], categories: Dict[str, str],
                 asset_class: str = "all") -> Tuple[List[str], List[str]]:
    """Return (kept, unknown) for the requested asset-class scope.

    `unknown` lists instruments absent from the frozen snapshot; callers must
    treat a non-empty `unknown` as an error.
    """
    if asset_class == "all":
        return sorted(insts), []
    if asset_class != "crypto":
        raise ValueError(f"unknown asset_class {asset_class!r}; known: {ASSET_CLASSES}")
    kept, unknown = [], []
    for i in insts:
        cat = categories.get(i)
        if cat is None:
            unknown.append(i)
        elif cat == CRYPTO_CATEGORY:
            kept.append(i)
    return sorted(kept), sorted(unknown)


def scope_summary(insts: Sequence[str], categories: Dict[str, str]) -> Dict[str, int]:
    """Count of instruments per category -- printed so the scope is never implicit."""
    out: Dict[str, int] = {}
    for i in insts:
        lab = classify(categories.get(i, "unknown"))
        out[lab] = out.get(lab, 0) + 1
    return dict(sorted(out.items()))


def main() -> None:                                            # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="freeze the OKX instrument classification")
    ap.add_argument("--build", action="store_true", help="query OKX and write the snapshot")
    ap.add_argument("--path", default=CATEGORY_FILE)
    ap.add_argument("--show", action="store_true", help="print the current snapshot summary")
    a = ap.parse_args()
    if a.build:
        p = build_category_snapshot(a.path)
        print(f"wrote {a.path}: {p['n_instruments']} instruments")
    if a.show or not a.build:
        cats = load_categories(a.path)
        from collections import Counter
        c = Counter(classify(v) for v in cats.values())
        print(f"snapshot {a.path}")
        for k, v in sorted(c.items()):
            print(f"  {k:12s} {v}")


if __name__ == "__main__":                                      # pragma: no cover
    main()
