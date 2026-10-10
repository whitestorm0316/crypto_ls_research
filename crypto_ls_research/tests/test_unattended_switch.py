"""无人值守开关：**「关着就绝不下单」必须是可证的。**

背景
----
这个开关把「一个守护进程在跑」变成「每天到点自动下真钱单」。它有三条必须
成立的规矩，每条都对应一种具体的伤害：

1. **默认关。** 一个从没被配置过的账户，不能因为恰好有个守护进程活着就开始
   下单。方向反了的话，后果不是"少交易一次"，是"在没人知情时下了一年的单"。
2. **关掉必须立刻生效。** 开关每个 tick 重读，不是构造时缓存 —— 需要重启才生效
   的开关，人会停止信任它，然后就不再关了。
3. **关掉必须是零成本且零动作。** 它排在数据刷新**之前**，所以关着的时候连行情
   都不去刷新（实测 0.0s）。「什么都不做」必须真的是什么都不做，不是
   "花 20 秒刷新再决定不下单"。

另外两条边界：
  * `resolve_auto_enabled` 对类型的判断必须**严格**。`"false"`、`"true"`、`1`
    都不是 `True`；一个手改过的 JSON 或 HTTP body 不保证给的是 bool。
  * `enabled`（许可位）与 `running`（进程活着）是**两个量**，页面要把它们分开说。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crypto_ls_research.execution.auto_trader import (          # noqa: E402
    AUTO_ENABLED_KEY, AutoTrader, resolve_auto_enabled,
)
from crypto_ls_research.execution.store import Store             # noqa: E402


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """`Store` 的目录是模块常量 `LIVE_DIR`，**没有环境变量可覆写** —— 所以测试
    必须 monkeypatch 它本身，否则会写到真实的 `artifacts/live/<mode>/` 上去。

    （这一点是实测发现的：我原先假设有个 `CRYPTO_ARTIFACTS_DIR`，结果测试
    直接改到了真实 live 账户的开关文件上。测试必须证明它没有副作用。）
    """
    import crypto_ls_research.execution.store as store_mod
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(tmp_path / "live"))
    return store_mod


# ---------------------------------------------------------------------------
# 1. 解析：默认关，且类型严格
# ---------------------------------------------------------------------------
def test_absent_or_unreadable_config_means_off():
    """读不到 = 关。fail closed，绝不"沿用上次看到的"。"""
    assert resolve_auto_enabled({}) is False
    assert resolve_auto_enabled(None) is False
    assert resolve_auto_enabled({"other": 1}) is False
    # 不是 dict 的一切（损坏的 JSON 会被读成别的类型）
    for junk in ("", [], 0, 1.5, "enabled", object()):
        assert resolve_auto_enabled(junk) is False, f"{junk!r} 应读成关"


def test_only_literal_true_enables():
    """`is True` 而不是真值测试 —— `"false"` 是真值，会静默把开关打开。"""
    assert resolve_auto_enabled({AUTO_ENABLED_KEY: True}) is True
    for not_true in ("false", "true", "1", "0", 1, 0, None, [], {}, -1):
        assert resolve_auto_enabled({AUTO_ENABLED_KEY: not_true}) is False, \
            f"{not_true!r} 不是布尔 True，必须读成关"


# ---------------------------------------------------------------------------
# 2. 持久化：读写往返，且和心脏跳动的 auto.json 不是同一个文件
# ---------------------------------------------------------------------------
def test_store_roundtrip(isolated_store):
    st = Store("live")
    assert st.load_auto_config() == {}          # 从未配置 == 空 dict（不是 None）
    assert resolve_auto_enabled(st.load_auto_config()) is False

    st.save_auto_config({AUTO_ENABLED_KEY: True})
    assert resolve_auto_enabled(st.load_auto_config()) is True
    # 往返后带上时间戳，但不污染业务键
    cfg = st.load_auto_config()
    assert cfg[AUTO_ENABLED_KEY] is True
    assert "_saved" in cfg and "_mode" in cfg


def test_switch_file_is_not_the_heartbeat_file():
    """开关不能住在 `auto.json` 里 —— 那个文件守护进程每轮都重写。

    一个住在"每天被重写 48 次"的文件里的设置，就是一个会丢的设置。
    """
    st = Store("live")
    assert os.path.basename(st.auto_path()) != "auto.json"
    assert st.auto_path() != st.path("auto.json")


# ---------------------------------------------------------------------------
# 3. 守护进程：关着时跳过，且**不碰任何东西**
# ---------------------------------------------------------------------------
class _Recorder:
    """只记录被调用过什么的最小替身。"""

    def __init__(self):
        self.calls = []

    def data_staleness(self):
        self.calls.append("data_staleness")
        return {"bar_age_hours": 99.0, "stale": True}

    def target(self, **kw):
        self.calls.append("target")
        raise AssertionError("开关关着时不该去算信号")


def test_disabled_skips_before_touching_anything(monkeypatch, tmp_path):
    """关着 ⇒ `skip / auto_disabled`，且**没有刷新、没有算信号、没有下单**。

    只断言 reason 是不够的：一个"先刷新 20 秒再跳过"的实现同样会给出那个
    reason。所以同时钉住 `engine` 一次都没被碰过。
    """
    eng = _Recorder()
    bot = AutoTrader(mode="live", allow_live=True, engine=eng,
                     notifier=_NullNotifier())

    # 开关关着（默认）
    monkeypatch.setattr(bot.store, "load_auto_config", lambda: {})
    monkeypatch.setattr(
        "crypto_ls_research.execution.auto_trader.kill_switch_on",
        lambda: False)

    out = bot.once()
    assert out["action"] == "skip"
    assert out["reason"] == "auto_disabled"
    assert out["auto_enabled"] is False
    assert eng.calls == [], (
        f"开关关着却动了引擎：{eng.calls} —— 「什么都不做」必须真的是什么都不做")


def test_enabled_does_not_skip_on_the_switch(monkeypatch):
    """反向断言：开着就不该以 `auto_disabled` 为由跳过。

    没有这一条，上面那条在"函数永远跳过"时也是绿的 —— 假守卫。
    这里只需要证明**它走过了那道闸门**（随后因数据陈旧而停在别的原因上）。
    """
    eng = _Recorder()
    bot = AutoTrader(mode="live", allow_live=True, engine=eng,
                     notifier=_NullNotifier())
    monkeypatch.setattr(bot.store, "load_auto_config",
                        lambda: {AUTO_ENABLED_KEY: True})
    monkeypatch.setattr(
        "crypto_ls_research.execution.auto_trader.kill_switch_on",
        lambda: False)

    out = bot.once()
    assert out.get("auto_enabled") is True
    assert out["reason"] != "auto_disabled", (
        "开关是开着的，却以 auto_disabled 跳过 —— 开关没被读到")
    # 它确实往下走了（陈旧行情 → 试图刷新，于是碰了数据层）
    assert "data_staleness" in eng.calls


def test_switch_is_read_fresh_every_tick(monkeypatch):
    """每轮重读。构造时缓存 ⇒ 页面上的开关看起来"没反应"。"""
    bot = AutoTrader(mode="live", allow_live=True, engine=_Recorder(),
                     notifier=_NullNotifier())
    seen = {"n": 0}

    def fake():
        seen["n"] += 1
        return {}

    # 计数只证明"读了多次"，不证明"读的是**当前**盘上的值"。真正的性质是：
    # 把盘上的值改掉，**下一轮立刻反映**，不需要重启进程。
    state = {"enabled": False}

    def fake():
        seen["n"] += 1
        return {AUTO_ENABLED_KEY: state["enabled"]}

    monkeypatch.setattr(bot.store, "load_auto_config", fake)
    monkeypatch.setattr(
        "crypto_ls_research.execution.auto_trader.kill_switch_on",
        lambda: False)

    first = bot.once()
    assert first["reason"] == "auto_disabled", "初始为关，应跳过"

    # 盘上改成开 —— 同一个 bot 实例，不重建
    state["enabled"] = True
    second = bot.once()
    assert second["reason"] != "auto_disabled", (
        "盘上开关已改成开，同一个进程却仍按旧的跳过 —— "
        "开关没有每个 tick 重读（构造时缓存了）")
    assert seen["n"] >= 2, (
        f"开关只被读了 {seen['n']} 次 —— 应该是每个 tick 都重读")


class _NullNotifier:
    """不发任何东西，但接口齐全（`once()` 会调 `describe` / `send`）。"""

    def describe(self):
        return {"enabled": False, "provider": "none", "ready": False,
                "url": "", "notify_on": [], "modes": [],
                "dedup_sec": 0, "secret_set": False, "config_path": ""}

    def enabled_for(self, action, mode=None):
        return False

    def send(self, *a, **kw):
        return {"ok": False, "skipped": "测试"}

    def config_path(self):
        return "/dev/null"


# ---------------------------------------------------------------------------
# 4. 页面/契约：enabled 与 running 必须都能被读到
# ---------------------------------------------------------------------------
def test_api_exposes_enabled_and_running_separately(isolated_store, monkeypatch):
    """`enabled` 与 `running` 是两个量，必须分别暴露。

    合并成一个 `on` 就会出现最危险的那种误读：「显示已开启」但其实没有进程，
    或者反过来。`will_trade` 是两者之与，是"这账户会不会真的自己下单"的答案。
    """
    import webapp.live_api as api

    monkeypatch.setattr(api.auto_ctl, "is_running", lambda m: False)
    monkeypatch.setattr(api.auto_ctl, "read_state", lambda m: {})
    monkeypatch.setattr(api.auto_ctl, "read_ctl", lambda *a, **k: {})
    monkeypatch.setattr(api.auto_ctl, "grid_for", lambda m: {})

    import crypto_ls_research.execution.store as store_mod
    from webapp import auto_schedule
    monkeypatch.setattr(api, "_REGISTRY", auto_schedule.Registry())
    st = api.Store("live")
    for k in ("enabled", "running", "will_trade", "config_path", "at", "ran_today"):
        assert k in api._auto_view("live"), f"缺少 {k}"

    # ⚠️ 必须**四种组合都走一遍**。只测 (False, False) 会让
    # `will_trade = enabled and running` 与 `will_trade = enabled` 同值 ——
    # 断言恒真，变异测试当场抓到过这条假守卫。
    cases = [
        # (落盘的开关, 进程是否在跑, 期望 will_trade)
        (False, False, False),
        (False, True,  False),   # 进程在跑但没开 ⇒ 正确地不下单
        (True,  False, False),   # 开了但没进程 ⇒ **不会**下单（最危险的误读）
        (True,  True,  True),    # 唯一会真正交易的状态
    ]
    # `running` 现在指**后端调度线程**，不再是 `auto_ctl` 那个守护进程 ——
    # 所以用假的 Scheduler 替身来驱动它（mock 错对象会让整条断言测的是空气）。
    class _FakeSched:
        def __init__(self, alive):
            self.at = "11:00"
            self.last = {}
            self._alive = alive

        def is_alive(self):
            return self._alive

        def already_ran_today(self):
            return False

    class _FakeReg:
        def __init__(self, alive):
            self._s = _FakeSched(alive)

        def get(self, mode):
            return self._s

        def status(self):
            return []

    for enabled, running, want in cases:
        st.save_auto_config({AUTO_ENABLED_KEY: enabled})
        monkeypatch.setattr(api, "_REGISTRY", _FakeReg(running))
        monkeypatch.setattr(api.auto_ctl, "read_state", lambda m: {})
        monkeypatch.setattr(api.auto_ctl, "read_ctl", lambda *a, **k: {})
        monkeypatch.setattr(api.auto_ctl, "grid_for", lambda m: {})
        v = api._auto_view("live")
        assert v["enabled"] is enabled, f"enabled 读错：{v}"
        assert v["running"] is running, f"running 读错：{v}"
        assert v["will_trade"] is want, (
            f"enabled={enabled} running={running} 时 will_trade 应为 {want}，"
            f"实际 {v['will_trade']}")


def test_enabling_live_needs_no_confirm_phrase(isolated_store, monkeypatch):
    """**没有确认短语闸门** —— 用户 2026-10-10 明确裁掉。

    这里钉住的是「不再需要短语」这个决定本身：如果哪天有人把闸门加回来，
    这条测试会红，逼他同时更新 `AUTO_TRADER_README.md` 与页面上的警告文案。
    （代价由按钮旁的显式警告承担：开启实盘 = 每天无人复核地下真钱单。）

    ⚠️ 这条**不是**"实盘可以随便开"的意思 —— 风控闸门（换手/名义额/到期）
    仍然是最后一道墙，那部分在别处测。
    """
    import webapp.live_api as api

    monkeypatch.setattr(api.auto_ctl, "is_running", lambda m: False)
    monkeypatch.setattr(api.auto_ctl, "read_state", lambda m: {})
    monkeypatch.setattr(api.auto_ctl, "read_ctl", lambda *a, **k: {})
    monkeypatch.setattr(api.auto_ctl, "grid_for", lambda m: {})
    monkeypatch.setattr(api, "ensure_scheduler", lambda m: None, raising=True)

    # 不带 confirm 也能开
    body, code = api._auto_set_enabled({"mode": "live"}, True)
    assert code == 200, f"实盘开启不该再要求短语，实际 {code}: {body}"
    assert body["enabled"] is True
    assert api.Store("live").load_auto_config()[AUTO_ENABLED_KEY] is True

    # 关闭同样不需要
    body, code = api._auto_set_enabled({"mode": "live"}, False)
    assert code == 200 and body["enabled"] is False


def test_an_unknown_mode_is_still_rejected(isolated_store, monkeypatch):
    """**去掉的是确认短语，不是参数校验。**

    容易顺手一起删掉的那种：`mode` 必须仍是 paper/demo/live 之一。一个写错的
    mode 会让开关落到一个谁也不读的目录上，而页面显示"已开启"。
    """
    import webapp.live_api as api
    body, code = api._auto_set_enabled({"mode": "liv"}, True)
    assert code == 400, f"未知 mode 必须拒绝，实际 {code}"


def test_enable_response_reports_disk_not_request(isolated_store, monkeypatch):
    """响应必须回**落盘后重读**的值，不是回显请求。

    回显会让一次失败的写盘看起来像成功 —— 而这里的失败模式是最坏的一种：
    页面说「已开启」，文件其实还是关的（或反过来）。
    """
    import webapp.live_api as api

    monkeypatch.setattr(api.auto_ctl, "is_running", lambda m: False)
    monkeypatch.setattr(api.auto_ctl, "read_state", lambda m: {})
    monkeypatch.setattr(api.auto_ctl, "read_ctl", lambda *a, **k: {})
    monkeypatch.setattr(api.auto_ctl, "grid_for", lambda m: {})

    # 让写盘"成功"但内容不生效（模拟被别的东西覆盖/守卫拒绝）
    st = api.Store("demo")
    real = st.save_auto_config

    def lying_save(d):
        return real({})          # 写下去的是空的

    monkeypatch.setattr(st, "save_auto_config", lying_save)
    monkeypatch.setattr(api, "Store", lambda m: st)

    body, code = api._auto_set_enabled({"mode": "demo"}, True)
    assert code == 200
    assert body["requested"] is True
    assert body["enabled"] is False, (
        "请求说开、盘上是关，响应必须报『关』—— 回显请求会掩盖写盘失败")
