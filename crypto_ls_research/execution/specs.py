"""Contract specifications -- the bridge from "target notional" to "legal order".

An order size on OKX is an integer multiple of `lotSz` contracts, floored at
`minSz`, and one contract is worth `ctVal * price` USDT for a linear swap.  So

    contracts = target_notional / (ctVal * price)

rounded to the lot grid.  Two consequences the research backtest never has to
face, and which this project already measured the cost of (`报告 §16`):

* **The minimum order is a fixed dollar amount per instrument**, independent of
  capital: `minSz * ctVal * price`.  In this pool it is $0.02–$15.39, median
  ~$1.04.  A $70 account cannot place a 0.3%-of-NAV leg on several of the
  larger-contract names at all.
* **Rounding is a one-sided error.**  Whatever is not rounded away is spread
  over fewer names, so the realised book is systematically *less* diversified
  than the target.  That is why the planner reports coverage and weight error
  next to every plan instead of just the order list.

Only **USDT-margined linear swaps** are supported.  Inverse contracts
(`BTC-USD-SWAP`) settle in coin, which makes "notional" and "weight" mean
something different and would silently break the market-neutral construction.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

SPEC_FILE = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "..")), "data_cache", "meta", "swap_specs.json")

SPECS_CACHE: Dict[str, "InstSpec"] = {}


@dataclass(frozen=True)
class InstSpec:
    inst_id: str
    ct_val: float            # base-currency units per contract
    ct_val_ccy: str
    lot_sz: float            # order size granularity, in contracts
    min_sz: float            # minimum order size, in contracts
    tick_sz: float
    state: str
    max_lever: float
    settle_ccy: str
    max_mkt_sz: Optional[float] = None
    max_lmt_sz: Optional[float] = None

    def notional_per_contract(self, price: float) -> float:
        return self.ct_val * float(price)

    def min_notional(self, price: float) -> float:
        """Smallest order the exchange will accept, in USDT.  Capital-independent."""
        return self.min_sz * self.notional_per_contract(price)

    def contracts_for(self, notional: float, price: float, dust_ratio: float = 0.5) -> float:
        """Signed contract count for a signed target notional.

        A delta below `dust_ratio * minSz` is dropped to zero rather than rounded
        up to `minSz`: rounding it up would create a position out of noise, and
        the whole point of a market-neutral book is that its small legs are not
        invented.  Anything at or above the threshold is snapped *up* to `minSz`
        so the order is legal.

        Returns 0.0 -- never NaN -- for any unusable input.  A NaN price would
        otherwise propagate: `max(nan, minSz)` is `nan`, so a stale mark could
        turn into a NaN order size instead of "no order".
        """
        try:
            p = float(price)
            n = float(notional)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(p) or not math.isfinite(n) or p <= 0:
            return 0.0
        per_ct = self.notional_per_contract(p)
        if not math.isfinite(per_ct) or per_ct <= 0:
            return 0.0
        raw = abs(n) / per_ct
        if not math.isfinite(raw) or raw <= 0:
            return 0.0
        if raw < dust_ratio * self.min_sz:
            return 0.0
        lots = round(raw / self.lot_sz) * self.lot_sz if self.lot_sz > 0 else raw
        lots = max(lots, self.min_sz)
        if not math.isfinite(lots) or lots <= 0:
            return 0.0
        # kill floating-point dust introduced by the lot grid (e.g. 3.0000000004)
        lots = round(lots, 10)
        return lots * (1.0 if n >= 0 else -1.0)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _num(v, default=None):
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def load_specs(refresh: bool = False, proxy: Optional[str] = None) -> Dict[str, InstSpec]:
    """Instrument specs, USDT-linear only.  Cached in-process."""
    global SPECS_CACHE
    if SPECS_CACHE and not refresh:
        return SPECS_CACHE
    if refresh or not os.path.exists(SPEC_FILE):
        refresh_specs(proxy=proxy)
    with open(SPEC_FILE, "r", encoding="utf-8") as f:
        rows = json.load(f)
    out: Dict[str, InstSpec] = {}
    for r in rows:
        if r.get("instType") != "SWAP":
            continue
        if str(r.get("settleCcy", "")).upper() != "USDT":
            continue
        inst = r.get("instId")
        ct = _num(r.get("ctVal"))
        if not inst or not ct or ct <= 0:
            continue
        out[inst] = InstSpec(
            inst_id=inst, ct_val=ct, ct_val_ccy=str(r.get("ctValCcy", "")),
            lot_sz=_num(r.get("lotSz"), 0.0) or 0.0,
            min_sz=_num(r.get("minSz"), 0.0) or 0.0,
            tick_sz=_num(r.get("tickSz"), 0.0) or 0.0,
            state=str(r.get("state", "")),
            max_lever=_num(r.get("lever"), 1.0) or 1.0,
            settle_ccy=str(r.get("settleCcy", "")),
            max_mkt_sz=_num(r.get("maxMktSz")), max_lmt_sz=_num(r.get("maxLmtSz")),
        )
    SPECS_CACHE = out
    return out


def refresh_specs(proxy: Optional[str] = None) -> int:
    """Re-freeze the spec snapshot from `GET /api/v5/public/instruments`."""
    from ..data.okx_client import OKXClient
    cli = OKXClient(**({"proxy": proxy} if proxy else {}))
    rows = cli.instruments("SWAP")
    os.makedirs(os.path.dirname(SPEC_FILE), exist_ok=True)
    tmp = SPEC_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    os.replace(tmp, SPEC_FILE)
    global SPECS_CACHE
    SPECS_CACHE = {}
    return len(rows)


def subset(insts: List[str], specs: Optional[Dict[str, InstSpec]] = None
           ) -> Dict[str, InstSpec]:
    specs = specs or load_specs()
    return {i: specs[i] for i in insts if i in specs}
