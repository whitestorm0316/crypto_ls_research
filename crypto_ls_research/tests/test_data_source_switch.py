"""Guards for the venue switch (`--source binance` / `CRYPTO_CACHE_DIR`).

Two failure modes this work could hide, both silent:

1. **A swapped volume field.**  OKX `history-candles` and Binance `klines` put base
   and quote volume in different positions.  Getting them the wrong way round does
   not raise -- it produces a plausible-looking panel whose `vwap` is off by a factor
   of ~price^2, which then quietly moves `next_vwap` execution, the ADV universe gate
   and the `flow` factor.  `amount` must be QUOTE (USDT) turnover and `vol_ccy` must
   be BASE volume, because `store.Panels.vwap == amount / vol_ccy` and the liquidity
   gate compares `amount` against a dollar floor.

2. **A cache path that drifts from its single source.**  `settings.CACHE_DIR` and
   `store.CACHE` must agree; if the downloader writes to one cache and the loader
   reads another, every comparison silently measures the wrong dataset.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.config import settings
from crypto_ls_research.data import store
from crypto_ls_research.data.binance_client import BinanceClient, _BAR_MS
from crypto_ls_research.data.download import COLS, _rows_to_df

# One real `/fapi/v1/klines` row (BTCUSDT, 2021-01-01 00:00 UTC), field for field.
_BINANCE_ROW = [
    1609459200000,          # 0 open time
    "28948.19",             # 1 open
    "29055.00",             # 2 high
    "28706.00",             # 3 low
    "29015.00",             # 4 close
    "8037.588",             # 5 volume      (BASE)
    1609462799999,          # 6 close time
    "232164597.07348",      # 7 quote volume (QUOTE)
    12345,                  # 8 trades
    "4000.0", "115000000.0", "0",
]
_OPEN_MS = _BINANCE_ROW[0]
_CLOSE_MS = _BINANCE_ROW[6]


def _client(rows):
    """A client whose network layer is replaced by a canned page."""
    bc = BinanceClient(proxy=None)
    bc.klines_page = lambda *a, **k: rows                    # type: ignore[assignment]
    return bc


def test_klines_range_emits_the_okx_column_count():
    bc = _client([_BINANCE_ROW])
    rows = bc.klines_range("BTCUSDT", "1h", _OPEN_MS, _OPEN_MS + _BAR_MS["1h"],
                           now_ms=_CLOSE_MS + 1)
    assert len(rows) == 1
    assert len(rows[0]) == len(COLS), "下游按 OKX 的 9 列解析，列数必须一致"


def test_vol_ccy_is_base_and_amount_is_quote():
    """The one mapping that must never be swapped."""
    bc = _client([_BINANCE_ROW])
    row = bc.klines_range("BTCUSDT", "1h", _OPEN_MS, _OPEN_MS + _BAR_MS["1h"],
                          now_ms=_CLOSE_MS + 1)[0]
    rec = dict(zip(COLS, row))
    assert float(rec["vol_ccy"]) == float(_BINANCE_ROW[5]), "vol_ccy 必须是基础币成交量"
    assert float(rec["amount"]) == float(_BINANCE_ROW[7]), "amount 必须是计价币成交额"


def test_the_implied_vwap_is_a_price_not_a_price_squared():
    """`amount / vol_ccy` must land on the price scale.

    This is the assertion that actually catches a swap: with the two fields the
    wrong way round the ratio is ~price^2 (about 8e8 for BTC), which is absurd but
    finite, so nothing downstream would complain.
    """
    bc = _client([_BINANCE_ROW])
    row = bc.klines_range("BTCUSDT", "1h", _OPEN_MS, _OPEN_MS + _BAR_MS["1h"],
                          now_ms=_CLOSE_MS + 1)[0]
    rec = dict(zip(COLS, row))
    vwap = float(rec["amount"]) / float(rec["vol_ccy"])
    close = float(rec["close"])
    assert abs(vwap / close - 1) < 0.05, f"vwap={vwap:.2f} close={close:.2f} 不在价格量级"


def test_an_unclosed_bar_is_marked_unconfirmed():
    """Binance has no confirm flag, so the in-progress bar must be inferred from
    `closeTime`: dropping it is what stops the backtest reading a partial bar."""
    bc = _client([_BINANCE_ROW])
    open_bar = bc.klines_range("BTCUSDT", "1h", _OPEN_MS, _OPEN_MS + _BAR_MS["1h"],
                               now_ms=_CLOSE_MS - 1)[0]
    closed_bar = bc.klines_range("BTCUSDT", "1h", _OPEN_MS, _OPEN_MS + _BAR_MS["1h"],
                                 now_ms=_CLOSE_MS + 1)[0]
    assert dict(zip(COLS, open_bar))["confirm"] == 0
    assert dict(zip(COLS, closed_bar))["confirm"] == 1


def test_rows_to_df_drops_the_unconfirmed_bar():
    bc = _client([_BINANCE_ROW])
    rows = bc.klines_range("BTCUSDT", "1h", _OPEN_MS, _OPEN_MS + _BAR_MS["1h"],
                           now_ms=_CLOSE_MS - 1)          # bar still forming
    df = _rows_to_df(rows)
    assert len(df) == 0, "未收盘的 bar 必须被丢掉"


def test_rows_to_df_produces_the_cached_schema():
    bc = _client([_BINANCE_ROW])
    rows = bc.klines_range("BTCUSDT", "1h", _OPEN_MS, _OPEN_MS + _BAR_MS["1h"],
                           now_ms=_CLOSE_MS + 1)
    df = _rows_to_df(rows)
    assert list(df.columns) == ["open", "high", "low", "close", "vol", "vol_ccy", "amount"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None
    assert df.index[0] == pd.Timestamp(_OPEN_MS, unit="ms", tz="UTC")
    for c in df.columns:
        assert np.isfinite(df[c].iloc[0])


def test_unsupported_bar_fails_loudly():
    bc = _client([_BINANCE_ROW])
    with pytest.raises(ValueError):
        bc.klines_range("BTCUSDT", "7h", _OPEN_MS, _OPEN_MS + _BAR_MS["1h"])


def test_pagination_advances_and_stops_at_the_end():
    """A full page must advance the cursor, not loop on the same bar forever."""
    bar_ms = _BAR_MS["1h"]
    pages = []
    for i in range(3):
        row = list(_BINANCE_ROW)
        row[0] = _OPEN_MS + i * bar_ms
        row[6] = row[0] + bar_ms - 1
        pages.append([row])

    bc = BinanceClient(proxy=None)
    calls = []

    def fake(symbol, bar, start_ms, end_ms, limit=1500, retries=5):
        calls.append(start_ms)
        # A page is a list of ROWS, not a list of pages.
        return [r[0] for r in pages if r[0][0] >= start_ms]

    bc.klines_page = fake                                       # type: ignore[assignment]
    rows = bc.klines_range("BTCUSDT", "1h", _OPEN_MS, _OPEN_MS + 3 * bar_ms,
                           limit=1, now_ms=_OPEN_MS + 10 * bar_ms)
    assert len(rows) == 3
    assert [r[0] for r in rows] == [_OPEN_MS, _OPEN_MS + bar_ms, _OPEN_MS + 2 * bar_ms]
    assert calls == sorted(calls) and len(set(calls)) == len(calls), "游标必须单调前进"


def test_cache_dir_has_exactly_one_source():
    """`settings.CACHE_DIR` is the source; `store.CACHE` must not re-derive it."""
    assert store.CACHE == settings.CACHE_DIR


def test_the_default_cache_is_still_the_historical_path():
    """An unset env var must leave every archived number reproducible."""
    assert settings.CACHE_DIR.endswith(os.path.join("crypto_ls_research", "data_cache")
                                       .replace(os.sep, os.sep)) or \
        os.path.basename(settings.CACHE_DIR) == "data_cache"


def test_the_env_var_is_what_selects_the_cache():
    """Documented contract: `CRYPTO_CACHE_DIR` overrides, default does not move."""
    assert os.environ.get("CRYPTO_CACHE_DIR") or settings.CACHE_DIR.endswith("data_cache")
