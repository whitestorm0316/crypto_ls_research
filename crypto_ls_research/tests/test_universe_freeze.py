"""`--insts-from`：冻结横截面必须能被原样复现。

为什么值得一条守卫
------------------
`load_panels(insts=None)` 读的是**整个缓存目录**，而缓存是增量累积的：下过什么
就永远留在里面，越用越宽。于是同一份代码 + 同一组参数，隔几周再跑就是另一个
横截面。2026-10-19 实测：缓存 161 个标的时 Sharpe 1.696，补到 205 个之后 2.014
—— 差 0.32，日志里一个字都没有。

所以每次运行都会把实际参与的那份清单写进 `00c_universe_insts.json`，`--insts-from`
再把它读回来。这条守卫钉住三件事：**求交**（清单里有、缓存里没有的不能混进来）、
**保序**（清单的顺序就是回测的顺序，排序一变就不是同一次运行）、**三种形状都能读**。
"""
from __future__ import annotations

import json

import pytest

from crypto_ls_research.run import research as rs_mod

CACHED = ["AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"]


@pytest.fixture
def fake_cache(monkeypatch):
    """把「缓存里有哪些标的」固定住，免得测试依赖真实 data_cache。"""
    monkeypatch.setattr(rs_mod, "list_cached_insts", lambda bar: list(CACHED))


def _write(tmp_path, obj):
    p = tmp_path / "00c_universe_insts.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_intersects_with_cache_and_keeps_order(tmp_path, fake_cache):
    # 清单里 BBB 在前、CCC 在中间、还混了一个缓存里没有的 ZZZ
    path = _write(tmp_path, {"insts": ["CCC-USDT-SWAP", "ZZZ-USDT-SWAP",
                                       "AAA-USDT-SWAP"]})
    got, missing = rs_mod.load_frozen_insts(path, "1h")
    assert got == ["CCC-USDT-SWAP", "AAA-USDT-SWAP"]      # 顺序跟清单，不跟缓存
    assert missing == ["ZZZ-USDT-SWAP"]                   # 缺的要报出来，不能静默丢


def test_accepts_bare_json_array(tmp_path, fake_cache):
    path = _write(tmp_path, ["BBB-USDT-SWAP"])
    got, missing = rs_mod.load_frozen_insts(path, "1h")
    assert got == ["BBB-USDT-SWAP"] and missing == []


def test_accepts_00b_kept_insts_shape(tmp_path, fake_cache):
    """`00b_universe_scope.json` 用 `kept_insts`，也该能直接指回来。"""
    path = _write(tmp_path, {"kept_insts": ["AAA-USDT-SWAP", "BBB-USDT-SWAP"]})
    got, missing = rs_mod.load_frozen_insts(path, "1h")
    assert got == ["AAA-USDT-SWAP", "BBB-USDT-SWAP"] and missing == []


def test_empty_intersection_is_not_silently_empty(tmp_path, fake_cache):
    """清单与缓存零交集时 `got` 是空列表 —— 调用方必须能看见并中止。"""
    path = _write(tmp_path, {"insts": ["ZZZ-USDT-SWAP"]})
    got, missing = rs_mod.load_frozen_insts(path, "1h")
    assert got == [] and missing == ["ZZZ-USDT-SWAP"]
