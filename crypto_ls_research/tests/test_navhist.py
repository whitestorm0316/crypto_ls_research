"""Tests for the rebuilt account-NAV history.

The point of these tests is that the curve is *derived*, not measured, so every
way the derivation can go quietly wrong has to be pinned:

* the backward walk must invert a forward simulation exactly (it is the only
  property that makes the curve trustworthy at all);
* a fill stamped after the anchor must not be applied to the anchor's book;
* milliseconds-vs-seconds must not silently turn every fill into 1970;
* a missing `ctVal` or a missing candle series must be *named*, never treated
  as "size 0" / "price 0";
* with no ledger at all the result must be a degraded payload, not an exception
  or a flat plausible-looking line.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from crypto_ls_research.execution import navhist as nh
from crypto_ls_research.execution import store as store_mod
from crypto_ls_research.execution.specs import InstSpec

T0 = 1_790_000_000.0            # an arbitrary but fixed UTC instant
H = 3600.0


@pytest.fixture(autouse=True)
def _no_probe_cache():
    """`okx_reachable` caches its verdict for 90s; between tests that would
    make the *order* of the suite decide whether OKX is considered up."""
    nh._PROBE.update({"at": 0.0, "ok": None, "note": ""})
    yield
    nh._PROBE.update({"at": 0.0, "ok": None, "note": ""})


def spec(inst: str, ct: float) -> InstSpec:
    return InstSpec(inst_id=inst, ct_val=ct, ct_val_ccy="", lot_sz=1.0,
                    min_sz=1.0, tick_sz=0.001, state="live", max_lever=10.0,
                    settle_ccy="USDT")


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------
def test_normalize_ts_unifies_milliseconds_and_seconds():
    assert nh.normalize_ts(int(T0 * 1000)) == T0
    assert nh.normalize_ts(T0) == T0
    assert nh.normalize_ts("1790000000000") == T0
    for bad in (None, "", "abc", 0, -5, float("nan")):
        assert nh.normalize_ts(bad) is None


def test_signed_size_reads_side_not_posside():
    # a one-way (net) account labels every fill `posSide: net`; treating
    # posSide as the direction would flip the sign of every leg.
    sell = {"side": "sell", "posSide": "net", "sz": "3"}
    buy = {"side": "buy", "posSide": "net", "sz": "3"}
    assert nh.signed_size(sell) == -3.0
    assert nh.signed_size(buy) == 3.0
    assert nh.signed_size({"side": "sell", "sz": "0"}) == 0.0
    assert nh.signed_size({"side": "buy", "sz": None}) == 0.0


# ---------------------------------------------------------------------------
# the walk inverts a forward simulation
# ---------------------------------------------------------------------------
def test_backward_walk_recovers_the_forward_simulation_exactly():
    """Two bars, one fill: nav(t0) must come back as 1000.0.

    Forward: cash 1000, buy 2 @110 at t1 (fee 0.1), close 120 at t2 ->
    nav = 1000 - 0.1 + 2*(120-110) = 1019.9.
    """
    marks = {"X-USDT-SWAP": ([T0, T0 + H, T0 + 2 * H], [100.0, 110.0, 120.0])}
    fills = [{"instId": "X-USDT-SWAP", "side": "buy", "sz": "2", "px": 110.0,
              "fee": -0.1, "ts": T0 + H}]
    h = nh.reconstruct(
        mode="paper", positions_now={"X-USDT-SWAP": 2.0}, fills=fills,
        ct={"X-USDT-SWAP": 1.0}, marks=marks,
        anchor_ts=T0 + 2 * H, anchor_nav=1019.9,
        recorded=[{"ts": T0, "nav": 1000.0, "kind": "equity"}],
        start_s=T0, source="cache")
    assert [round(v, 6) for v in h.nav] == [1000.0, 999.9, 1019.9]
    assert h.max_pct == pytest.approx(0.0, abs=1e-12)
    assert h.checks and h.checks[0]["recon"] == pytest.approx(1000.0)


def test_fee_at_the_anchor_bar_moves_nav_back_out_of_the_fee():
    """The fee paid on the anchor bar raises the earlier NAV back up."""
    marks = {"X-USDT-SWAP": ([T0, T0 + H], [100.0, 100.0])}
    fills = [{"instId": "X-USDT-SWAP", "side": "buy", "sz": "1", "px": 100.0,
              "fee": -2.0, "ts": T0 + H}]
    h = nh.reconstruct(
        mode="paper", positions_now={"X-USDT-SWAP": 1.0}, fills=fills,
        ct={"X-USDT-SWAP": 1.0}, marks=marks,
        anchor_ts=T0 + H, anchor_nav=998.0, start_s=T0)
    assert h.nav[0] == pytest.approx(1000.0)


def test_fills_after_the_anchor_are_undone_before_walking():
    """A fill stamped after the anchor belongs to a later book, not this one."""
    marks = {"X-USDT-SWAP": ([T0, T0 + H], [100.0, 100.0])}
    fills = [{"instId": "X-USDT-SWAP", "side": "buy", "sz": "5", "px": 100.0,
              "fee": 0.0, "ts": T0 + 2 * H}]          # after the anchor
    h = nh.reconstruct(
        mode="live", positions_now={"X-USDT-SWAP": 5.0}, fills=fills,
        ct={"X-USDT-SWAP": 1.0}, marks=marks,
        anchor_ts=T0 + H, anchor_nav=1000.0, start_s=T0)
    # book at the anchor is flat, so a flat price series leaves NAV untouched
    assert [round(v, 6) for v in h.nav] == [1000.0, 1000.0]


def test_ctval_multiplier_is_what_moves_nav():
    marks = {"BTC-USDT-SWAP": ([T0, T0 + H], [50_000.0, 51_000.0])}
    h = nh.reconstruct(
        mode="live", positions_now={"BTC-USDT-SWAP": 0.5}, fills=[],
        ct={"BTC-USDT-SWAP": 0.01}, marks=marks,
        anchor_ts=T0 + H, anchor_nav=1005.0, start_s=T0)
    # 0.5 contracts * 0.01 BTC = 0.005 BTC; +1000 USD/BTC -> +5 USD
    assert h.nav[0] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# degraded inputs are named, never assumed away
# ---------------------------------------------------------------------------
def test_missing_candles_are_named_and_flagged():
    h = nh.reconstruct(
        mode="live", positions_now={"GHOST-USDT-SWAP": 3.0}, fills=[],
        ct={"GHOST-USDT-SWAP": 1.0}, marks={},
        anchor_ts=T0 + H, anchor_nav=1000.0, start_s=T0)
    assert h.missing_bars == ["GHOST-USDT-SWAP"]
    assert any("没有K线" in w for w in h.warnings)


def test_missing_ctval_is_named_not_treated_as_zero():
    marks = {"NOC-USDT-SWAP": ([T0, T0 + H], [100.0, 120.0])}
    h = nh.reconstruct(
        mode="live", positions_now={"NOC-USDT-SWAP": 4.0}, fills=[],
        ct={"NOC-USDT-SWAP": None}, marks=marks,
        anchor_ts=T0 + H, anchor_nav=1000.0, start_s=T0)
    assert any("ctVal" in w for w in h.warnings)
    assert h.nav[0] == pytest.approx(1000.0)     # not silently marked as 0s


def test_a_single_event_is_reported_instead_of_drawn():
    h = nh.reconstruct(
        mode="demo", positions_now={}, fills=[], ct={}, marks={},
        anchor_ts=T0, anchor_nav=5000.0, start_s=T0)
    assert h.partial is True
    assert h.ts == [T0]
    assert any("不足以重建" in w for w in h.warnings)


def test_recorded_points_use_the_run_log_too():
    rows = nh.recorded_points(
        [{"ts": T0, "nav": 1000.0}],
        [{"ts": T0 + H, "nav": 1005.0, "dry_run": True},
         {"ts": T0 + 2 * H, "nav": "bad"}])
    assert [r["kind"] for r in rows] == ["equity", "dry"]
    assert rows[1]["nav"] == 1005.0


def test_records_written_within_the_same_second_collapse_to_the_last():
    """execute() writes pre-trade / post-trade / settle marks about a second
    apart; comparing against the pre-trade one measures the trade, not the
    rebuild."""
    rows = [{"ts": T0, "nav": 100.0, "kind": "equity"},
            {"ts": T0 + 0.4, "nav": 99.0, "kind": "equity"},
            {"ts": T0 + 0.9, "nav": 98.9, "kind": "equity"}]
    out = nh._last_per_ts(rows)
    assert len(out) == 1 and out[0]["nav"] == 98.9


# ---------------------------------------------------------------------------
# candle loading
# ---------------------------------------------------------------------------
def _write_cache(root, inst: str, secs, closes):
    d = root / "candles" / "1h"
    d.mkdir(parents=True, exist_ok=True)
    idx = pd.to_datetime([int(s) * 10 ** 9 for s in secs], utc=True).as_unit("ms")
    pd.DataFrame({"close": closes}, index=idx).to_parquet(d / f"{inst}.parquet")


def test_cache_loader_reads_millisecond_index_as_seconds(tmp_path):
    """The cache index is datetime64[ms]; a bare astype('int64') is ms and
    would put every bar in 1970 (and make every mark lookup return None)."""
    _write_cache(tmp_path, "A-USDT-SWAP", [T0, T0 + H], [10.0, 11.0])
    marks, missing = nh.bars_from_cache(["A-USDT-SWAP"], "1h", cache_dir=str(tmp_path))
    assert missing == []
    assert marks["A-USDT-SWAP"][0] == [int(T0), int(T0 + H)]
    assert marks["A-USDT-SWAP"][1] == [10.0, 11.0]


def test_cache_loader_reports_missing_instruments(tmp_path):
    marks, missing = nh.bars_from_cache(["NOPE-USDT-SWAP"], "1h",
                                        cache_dir=str(tmp_path))
    assert marks == {} and missing == ["NOPE-USDT-SWAP"]


class _FakeOkx:
    """Serves `/market/candles` plus one history page, newest-first like OKX."""

    def __init__(self, rows, fail=False):
        # `rows` is `{instId: [row, ...]}`; a missing key is a delisted name.
        self.by_inst = {k: list(v) for k, v in (rows or {}).items()}
        self.fail = fail
        self.calls = []

    def get(self, path, params=None, **kw):
        self.calls.append((path, dict(params or {})))
        if self.fail:
            raise OSError("tunnel connection failed")
        rows = self.by_inst.get((params or {}).get("instId"), [])
        if not rows:
            raise KeyError("instrument does not exist")
        if path.endswith("/market/candles"):
            return rows[:nh._OKX_PAGE]
        after = float(params.get("after", 0)) / 1000.0
        return [r for r in rows if float(r[0]) / 1000.0 < after][:nh._OKX_PAGE]


def _okx_row(sec, close):
    return [str(int(sec * 1000)), "0", "0", "0", str(close), "0", "0", "0", "1"]


def test_okx_loader_pages_backwards_and_keeps_the_forming_bar():
    rows = [_okx_row(T0 + i * H, 100.0 + i) for i in range(700, -1, -1)]
    cli = _FakeOkx({"A-USDT-SWAP": rows})
    marks, missing, notes = nh.bars_from_okx(
        ["A-USDT-SWAP"], "1h", T0, T0 + 700 * H, cli)
    assert missing == [] and notes == []
    ts = marks["A-USDT-SWAP"][0]
    assert ts[0] == int(T0) and ts[-1] == int(T0 + 700 * H)
    assert len(ts) == len(set(ts))              # boundary rows de-duplicated
    assert cli.calls[0][0].endswith("/market/candles")
    assert cli.calls[1][0].endswith("/history-candles")
    assert cli.calls[1][1]["after"] == str(int((T0 + 701 * H - 300 * H) * 1000))


def test_okx_source_is_named_and_falls_back_to_the_cache(tmp_path):
    _write_cache(tmp_path, "A-USDT-SWAP", [T0, T0 + H], [10.0, 11.0])
    good = _FakeOkx({"A-USDT-SWAP": [_okx_row(T0, 10.0), _okx_row(T0 + H, 11.0)]})
    marks, source, missing, notes = nh.load_marks(
        ["A-USDT-SWAP"], "1h", T0, T0 + H, use_okx=True, client=good,
        cache_dir=str(tmp_path))
    # 历史来自缓存、尾部来自 OKX —— 标签必须把这两件事都说出来，不能只喊 "okx"。
    assert source == "okx+cache" and missing == []
    assert marks["A-USDT-SWAP"][1] == [10.0, 11.0]
    assert any("本地缓存" in n and "OKX" in n for n in notes), notes

    bad = _FakeOkx({}, fail=True)
    marks, source, missing, notes = nh.load_marks(
        ["A-USDT-SWAP"], "1h", T0, T0 + H, use_okx=True, client=bad,
        cache_dir=str(tmp_path))
    assert source == "cache"
    assert any("OKX" in n for n in notes)
    assert marks["A-USDT-SWAP"][1] == [10.0, 11.0]


def test_a_name_with_no_cache_at_all_is_labelled_plain_okx(tmp_path):
    """没有任何本地历史的合约（新上市）只能整段向 OKX 要，标签就是 `okx`。"""
    rows = [_okx_row(T0 + i * H, 100.0 + i) for i in range(5, -1, -1)]
    cli = _FakeOkx({"NEW-USDT-SWAP": rows})
    marks, source, missing, _ = nh.load_marks(
        ["NEW-USDT-SWAP"], "1h", T0, T0 + 5 * H, use_okx=True, client=cli,
        cache_dir=str(tmp_path))
    assert source == "okx" and missing == []
    assert marks["NEW-USDT-SWAP"][0] == [T0 + i * H for i in range(6)]


def test_the_tail_fetch_stops_as_soon_as_it_overlaps_the_cache(tmp_path):
    """尾部只取一页就够：本地已经有历史，不该把 30 天窗口重下一遍。

    这是 20 秒卡顿的根因 —— 19 个合约 × 3 页 × 1.1s 全花在重复下载磁盘上
    已有的 bar。
    """
    cache_ts = [T0 + i * H for i in range(400)]
    _write_cache(tmp_path, "A-USDT-SWAP", cache_ts, [100.0] * 400)
    rows = [_okx_row(T0 + i * H, 100.0) for i in range(399, 501)]
    cli = _FakeOkx({"A-USDT-SWAP": rows})
    marks, source, _missing, _notes = nh.load_marks(
        ["A-USDT-SWAP"], "1h", cache_ts[0], T0 + 500 * H, use_okx=True,
        client=cli, cache_dir=str(tmp_path))
    assert source == "okx+cache"
    # 窗口起点比缓存起点还早？缓存第一条就得在，说明历史段确实来自缓存。
    assert marks["A-USDT-SWAP"][0][0] == int(cache_ts[0])
    assert marks["A-USDT-SWAP"][0][-1] == int(T0 + 500 * H)
    # 只取一页：探测一次（limit=2）+ 正式取尾部一次（limit=300）。**不是**把
    # 整个 30 天窗口按页翻一遍 —— 历史段是从磁盘拿的。
    tails = [c for c in cli.calls
             if c[0].endswith("/market/candles") and c[1].get("limit") == "300"]
    assert len(tails) == 1, cli.calls


def test_cache_and_okx_disagreeing_at_the_junction_refuses_to_splice(tmp_path):
    """拼接前必须在重叠 bar 上核对。对不上就整条退回缓存 —— 硬接会在
    mark-to-market 里变成一个凭空的 PnL 台阶。"""
    _write_cache(tmp_path, "A-USDT-SWAP", [T0, T0 + H, T0 + 2 * H], [10.0, 11.0, 12.0])
    # 重叠处 11.0 vs 99.0：这不是同一个序列。
    rows = [_okx_row(T0, 99.0), _okx_row(T0 + H, 99.0), _okx_row(T0 + 2 * H, 99.0),
            _okx_row(T0 + 3 * H, 99.0)]
    cli = _FakeOkx({"A-USDT-SWAP": rows})
    marks, source, _missing, notes = nh.load_marks(
        ["A-USDT-SWAP"], "1h", T0, T0 + 3 * H, use_okx=True, client=cli,
        cache_dir=str(tmp_path))
    assert source == "cache"
    assert marks["A-USDT-SWAP"][1] == [10.0, 11.0, 12.0]      # 原封不动
    assert any("不一致" in n for n in notes), notes


def test_splice_keeps_one_continuous_series_across_the_junction(tmp_path):
    """拼接后时间轴必须严格递增且无重复 —— 重复 bar 会让 walk 多走一步。"""
    cache_ts = [T0 + i * H for i in range(10)]
    # 价格从 1.0 起：close=0.0 是退化输入、会被丢掉（下面单独钉住），
    # 拿它当夹具会让这条测试测的是过滤而不是拼接。
    _write_cache(tmp_path, "A-USDT-SWAP", cache_ts,
                 [float(i + 1) for i in range(10)])
    rows = [_okx_row(T0 + i * H, float(i + 1)) for i in range(9, 15)]
    cli = _FakeOkx({"A-USDT-SWAP": rows})
    marks, _src, _m, _n = nh.load_marks(
        ["A-USDT-SWAP"], "1h", T0, T0 + 14 * H, use_okx=True, client=cli,
        cache_dir=str(tmp_path))
    ts, px = marks["A-USDT-SWAP"]
    assert ts == sorted(set(ts))
    assert len(ts) == len(px) == 15
    assert ts[9] == int(T0 + 9 * H) and px[9] == 10.0    # 交界处正好是重叠 bar


def test_a_non_positive_close_is_dropped_not_drawn_as_a_crash(tmp_path):
    """close=0 不是「跌到 0」而是缺数据；留在序列里会变成凭空的暴亏台阶。"""
    _write_cache(tmp_path, "A-USDT-SWAP", [T0, T0 + H, T0 + 2 * H],
                 [100.0, 0.0, 101.0])
    marks, missing = nh.bars_from_cache(["A-USDT-SWAP"], "1h",
                                        cache_dir=str(tmp_path))
    assert missing == []
    assert marks["A-USDT-SWAP"][0] == [int(T0), int(T0 + 2 * H)]
    assert marks["A-USDT-SWAP"][1] == [100.0, 101.0]


def test_probe_caches_its_verdict_so_a_dead_proxy_costs_one_timeout():
    good = _FakeOkx({"A-USDT-SWAP": [_okx_row(T0, 10.0), _okx_row(T0 + H, 11.0)]})
    ok, note = nh.okx_reachable(good, "A-USDT-SWAP", "1h", ttl=0)
    assert ok is True and note == ""

    bad = _FakeOkx({}, fail=True)
    ok, note = nh.okx_reachable(bad, "A-USDT-SWAP", "1h", ttl=0)
    assert ok is False and "OKX" in note

    # within the TTL the failed verdict is reused: the *second* client is never
    # asked, which is the whole point of the cache
    ok2, note2 = nh.okx_reachable(good, "A-USDT-SWAP", "1h", ttl=60)
    assert ok2 is False and note2 == note

    ok3, _ = nh.okx_reachable(None, None, "1h", ttl=0)
    assert ok3 is False


def test_a_name_only_in_the_cache_makes_the_source_mixed(tmp_path):
    _write_cache(tmp_path, "B-USDT-SWAP", [T0, T0 + H], [5.0, 6.0])
    cli = _FakeOkx({"A-USDT-SWAP": [_okx_row(T0, 10.0), _okx_row(T0 + H, 11.0)]})
    marks, source, missing, notes = nh.load_marks(
        ["A-USDT-SWAP", "B-USDT-SWAP"], "1h", T0, T0 + H, use_okx=True,
        client=cli, cache_dir=str(tmp_path))
    assert source == "mixed"
    assert set(marks) == {"A-USDT-SWAP", "B-USDT-SWAP"}
    assert missing == []
    assert any("B-USDT-SWAP" in n for n in notes)


# ---------------------------------------------------------------------------
# end-to-end over a real (temporary) store
# ---------------------------------------------------------------------------
@pytest.fixture()
def tmp_live(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(tmp_path / "live"))
    return tmp_path


def _seed(tmp_path, mode, *, positions, fills, equity):
    st = store_mod.Store(mode)
    # the ledger stores `{inst: {sz, avg_px}}`, not a bare number
    book = {i: {"sz": q, "avg_px": 100.0} for i, q in positions.items()}
    st.patch_state(positions=book, nav=equity[-1]["nav"])
    st.add_fills(fills)
    for r in equity:
        st.add_equity(r["nav"], ts=r["ts"])
    _write_cache(tmp_path, "X-USDT-SWAP",
                 [T0 + i * H for i in range(4)], [100.0, 110.0, 121.0, 108.9])
    return st


def test_a_bare_number_in_the_book_does_not_crash_the_panel(tmp_live):
    """A hand-edited / display-shaped book degrades the size, not the page."""
    assert nh._pos_sz({"sz": 3.0}) == 3.0
    assert nh._pos_sz(2.5) == 2.5
    assert nh._pos_sz("bad") is None
    assert nh._pos_sz(None) is None


def test_build_lands_on_the_recorded_nav_and_reports_coverage(tmp_live):
    """Anchor 119.9 at t3 must walk back onto the t1 record of 1000.0.

    Forward truth: buy 2 @110 at t1 (fee 0.1) -> 999.9; t2 close 121 ->
    +22; sell 2 @121 (fee 0.1) -> 1021.8; t3 close 108.9 -> flat book.
    """
    fills = [{"instId": "X-USDT-SWAP", "side": "buy", "sz": "2", "px": 110.0,
              "fee": -0.1, "ts": T0 + H, "ct_val": 1.0},
             {"instId": "X-USDT-SWAP", "side": "sell", "sz": "2", "px": 121.0,
              "fee": -0.1, "ts": T0 + 2 * H, "ct_val": 1.0}]
    _seed(tmp_live, "paper", positions={"X-USDT-SWAP": 0.0}, fills=fills,
          equity=[{"ts": T0, "nav": 1000.0}, {"ts": T0 + 3 * H, "nav": 1021.8}])
    h = nh.build("paper", bar="1h", use_okx=False, specs={"X-USDT-SWAP": spec("X", 1.0)},
                 cache_dir=str(tmp_live))
    d = h.to_dict()
    assert d["source"] == "cache"
    assert d["n"] == 4
    assert d["nav"][0] == pytest.approx(1000.0)
    assert d["nav"][-1] == pytest.approx(1021.8)
    assert d["max_pct"] == pytest.approx(0.0, abs=1e-12)
    assert d["coverage"]["span_secs"] == 3 * H


def test_build_with_no_ledger_degrades_instead_of_raising(tmp_live):
    h = nh.build("demo", use_okx=False, cache_dir=str(tmp_live))
    assert h.partial is True
    assert h.ts == []
    assert any("还没有任何净值记录" in w for w in h.warnings)


def test_build_flags_a_window_that_starts_before_the_first_fill(tmp_live):
    fills = [{"instId": "X-USDT-SWAP", "side": "buy", "sz": "2", "px": 110.0,
              "fee": -0.1, "ts": T0 + 2 * H, "ct_val": 1.0}]
    _seed(tmp_live, "live", positions={"X-USDT-SWAP": 2.0}, fills=fills,
          equity=[{"ts": T0, "nav": 1000.0}, {"ts": T0 + 3 * H, "nav": 1019.8}])
    h = nh.build("live", bar="1h", use_okx=False, specs={"X-USDT-SWAP": spec("X", 1.0)},
                 cache_dir=str(tmp_live))
    assert any("早于本地第一笔成交" in w for w in h.warnings)
    assert h.n_fills == 1


def test_build_rejects_unknown_bars_and_absurd_windows(tmp_live):
    with pytest.raises(ValueError):
        nh.build("paper", bar="7m", use_okx=False, cache_dir=str(tmp_live))
    with pytest.raises(ValueError):
        nh.build("paper", days=0, use_okx=False, cache_dir=str(tmp_live))
    with pytest.raises(ValueError):
        nh.build("paper", days=nh.MAX_DAYS + 1, use_okx=False,
                 cache_dir=str(tmp_live))


def test_days_window_trims_the_tail_only(tmp_live):
    fills = [{"instId": "X-USDT-SWAP", "side": "buy", "sz": "2", "px": 110.0,
              "fee": 0.0, "ts": T0 + H, "ct_val": 1.0}]
    _seed(tmp_live, "paper", positions={"X-USDT-SWAP": 2.0}, fills=fills,
          equity=[{"ts": T0, "nav": 1000.0}, {"ts": T0 + 3 * H, "nav": 1019.8}])
    h = nh.build("paper", bar="1h", days=1, use_okx=False,
                 specs={"X-USDT-SWAP": spec("X", 1.0)}, cache_dir=str(tmp_live))
    assert h.ts[0] >= T0 + 3 * H - 86400
    assert h.ts[-1] == T0 + 3 * H


def test_payload_is_json_serialisable(tmp_live):
    _seed(tmp_live, "paper", positions={"X-USDT-SWAP": 1.0}, fills=[],
          equity=[{"ts": T0, "nav": 1000.0}, {"ts": T0 + 3 * H, "nav": 1000.0}])
    h = nh.build("paper", bar="1h", use_okx=False,
                 specs={"X-USDT-SWAP": spec("X", 1.0)}, cache_dir=str(tmp_live))
    blob = json.dumps(h.to_dict())
    assert json.loads(blob)["n"] == 4
