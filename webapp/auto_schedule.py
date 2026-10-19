"""后端内置的每日调度器 —— 不依赖任何外部守护进程。

为什么会有这个模块
------------------
原来「每天 11:00 自动下单」只能靠一个**独立的守护进程**（`auto_trader`）实现，
而那个进程有个反复出问题的脆弱点：从 Web 控制台点按钮派生出来的子进程会随着
调用方的进程树一起被回收（日志断在半句、无 traceback、`is_running()` 几秒后变
回 False）。README 里为此专门写了一节「为什么网页按钮点不通」。

但控制台**自己**没有这个问题：它是双重 fork + `setsid()` 起的，父进程是
launchd，能长期活着。既然它本来就一直在跑，就没必要再拉一个进程去干这件事。

所以：把「到点执行一次」做成控制台内的一个后台线程。

设计约束（每条都对应一种具体的伤害）
------------------------------------
1. **开关关着 ⇒ 一个请求都不发。** 线程仍然在跑（它很便宜），但在执行前先查
   开关；关着就只更新状态、什么都不做。这条有测试钉住（断言 HTTP 层零调用）。
2. **幂等：同一天同一个窗口只执行一次。** 调度器每秒醒一次，如果不记账，
   11:00 之后的每一秒都会下一次单。落盘记 `last_run_day`，且**以「真的执行过」
   为准**，不是「打算执行」。
3. **不越过风控。** 它调用的是**和网页「一键准备 + 下单」完全同一条路径**
   （`live_api` 的 plan/execute 提交），所以 `check_plan` 的每个 block、
   换手闸门、到期限额统统照常生效。这里不自己下单、不自己放宽任何东西。
4. **错过不补。** 笔记本合盖、进程重启导致的错过，**不**在恢复后立刻补做 ——
   那会用一根旧信号下一批新单。错过就等下一个窗口，并在状态里说清楚。
5. **启动时自恢复。** 改代码重启控制台之后，开关仍然生效（读盘重建线程）。
   否则"重启一次就再也不自动了"，而页面上开关还是「已开启」—— 最坏的一种。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
from typing import Callable, Dict, List, Optional

#: 默认触发时刻（本地时间，"HH:MM"）。11:00 是回测执行价所在 bar 的收盘时刻；
#: 早于它会拿到还没确认的 bar，晚太多则信号变旧。可由环境变量覆盖，便于测试。
DEFAULT_AT = os.environ.get("LS_AUTO_AT", "11:00")

#: 每次唤醒的间隔。调度器本身极便宜（只比较两个字符串），所以可以密一点，
#: 让「到点」的抖动控制在几秒内 —— 而 11:00 这个时刻对 1h bar 是无所谓的。
TICK_SEC = float(os.environ.get("LS_AUTO_TICK", "1.0"))

#: 到点之后多久之内还算「赶上」，超过就算**错过**、当天不补做。
#:
#: ⚠️ 这条是实测逼出来的，不是洁癖。第一版只判 `now >= at`，于是我把控制台在
#: **15:25** 重启起来的那一刻，它立刻执行了一次（`runs.jsonl` 里那条
#: `15:25:31 n=0`）。那一次恰好没下单（目标跟持仓已一致），但机制是错的：
#: 任何一次下午的重启都会拿**当天 11:00 那根已经过期的 bar** 去下单。
#:
#: 错过就该等下一个窗口。这条与 `auto_trader` 的执行窗口护栏同源：
#: 补做用的是旧信号，而"晚一次"远比"用旧信号下一次"便宜。
CATCHUP_WINDOW_MIN = float(os.environ.get("LS_AUTO_CATCHUP_MIN", "10"))

#: 执行的落盘记录（每个账户一份），用来做「同一天只跑一次」的幂等判定。
STATE_FILE = "auto_schedule.json"


def _today() -> str:
    return _dt.date.today().isoformat()


def _now_hhmm() -> str:
    return time.strftime("%H:%M")


def _minutes_now() -> int:
    t = time.localtime()
    return t.tm_hour * 60 + t.tm_min


def _parse_hhmm(s: str) -> Optional[int]:
    """`"11:00"` -> 660（分钟）。解析不了返回 None（**不是** 0）。"""
    try:
        hh, mm = str(s).split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


class Scheduler:
    """一个账户的每日调度。线程很薄，真正的执行交给注入的 `runner`。

    `runner(mode) -> dict` 由调用方提供（`live_api` 注入它自己的
    plan+execute 提交），这样调度器**不需要**知道怎么下单 —— 也就没法
    在"顺手"的地方绕过风控。
    """

    def __init__(self, mode: str, runner: Callable[[str], dict], *,
                 state_path: Callable[[], str],
                 is_enabled: Callable[[], bool],
                 at: str = DEFAULT_AT,
                 tick: float = TICK_SEC,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.mode = mode
        self._runner = runner
        self._state_path = state_path
        self._is_enabled = is_enabled
        self.at = at
        self.tick = tick
        self._log = log or (lambda m: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        #: 供页面/状态接口读取的最近一次结论。内存态即可 —— 落盘的是幂等账。
        self.last: Dict = {}

    # -- 幂等账 -------------------------------------------------------------
    def _read_state(self) -> dict:
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            # 读不到 = 今天还没跑过。重复跑一次的代价远小于漏跑一天，
            # 但**幂等账只在"确实执行了"之后才写**，所以这里返回空是安全的。
            return {}

    def _write_state(self, d: dict) -> None:
        p = self._state_path()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, p)
        except OSError as e:                                     # noqa: BLE001
            # 写不进去必须说出来：写失败意味着幂等账丢失 ⇒ 明天可能重复执行。
            self._log(f"[auto] 写调度账失败（可能重复执行）：{e}")

    def already_ran_today(self) -> bool:
        return self._read_state().get("last_run_day") == _today()

    # -- 线程 ---------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name=f"auto-sched-{self.mode}", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout)

    def is_alive(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def _loop(self) -> None:
        self._log(f"[auto] {self.mode} 调度器启动：每天 {self.at}（开关关着时什么都不做）")
        while not self._stop.is_set():
            try:
                self._maybe_run()
            except Exception as e:                               # noqa: BLE001
                # 调度器必须比它调用的东西活得久：一次失败不能让线程死掉，
                # 否则"今天失败了"会变成"以后永远不跑了"，而页面上开关还是开着。
                self._log(f"[auto] {self.mode} 本轮出错（继续等待）：{type(e).__name__}: {e}")
            self._stop.wait(self.tick)

    def due(self) -> bool:
        """现在是不是该执行了？

        **是一个「窗口」而不是「阈值」**：`at` 起、`at + CATCHUP_WINDOW_MIN` 止。
        用 `now >= at` 的话，任何一次下午的重启都会立刻补跑一次 —— 拿当天已经
        过期的信号下单。错过就等下一个窗口（明天），而不是在傍晚替早上补作业。

        窗口下沿用 `>=` 而非常见的那种 `== 分钟`：调度器 tick 是秒级的，
        要求"正好等于 11:00 这一分钟"会在进程恰好卡顿时整天不执行，而漏一天的
        代价比早几秒大得多。
        """
        now = _minutes_now()
        start = _parse_hhmm(self.at)
        if start is None:
            return False
        return start <= now <= start + CATCHUP_WINDOW_MIN

    def _maybe_run(self) -> None:
        if not self._is_enabled():
            # 关着：连"到点了吗"都不必回答。这是最便宜的正确行为。
            self.last = {"skipped": "auto_disabled", "ts": time.time()}
            return
        if not self.due():
            return
        if self.already_ran_today():
            return
        self._run_once()

    def _run_once(self) -> None:
        self._log(f"[auto] {self.mode} 到点 {self.at}，开始执行（刷新行情 → 生成计划 → 下单）")
        t0 = time.time()
        try:
            res = self._runner(self.mode) or {}
        except SystemExit as e:
            # 🔴 和 `auto_trader.once()` 同一个坑：`signal.resolve_insts()` 的
            #    fail-loud 是 `raise SystemExit`（CLI 里是对的 —— 退出码 1），
            #    但 **`SystemExit` 不是 `Exception` 的子类** ⇒ 下面那个
            #    `except Exception` 接不住它 ⇒ 这个调度线程会死，
            #    而 `threading` 把线程里的 SystemExit **静默吞掉**。
            #    界面上只看到「调度器不在运行 / 今天没动静」，没有任何原因。
            #
            #    触发场景：有人全量下载（不带 `--reuse-pool`）把新合约拉进缓存，
            #    而 `inst_category.json` 还没重建。
            self.last = {
                "ok": False,
                "error": f"宇宙分类失败：{e} —— "
                         f"重建后同一窗口会自动重试："
                         f"python -m crypto_ls_research.data.asset_class --build",
                "ts": time.time()}
            self._log(f"[auto] {self.mode} 宇宙分类失败（不写幂等账，"
                      f"下一轮重试）：{e}")
            return
        except Exception as e:                                   # noqa: BLE001
            # 例外只记在状态里、**不写幂等账** —— 没成功就该允许在同一窗口内
            # 重试（下一次 tick 会再进来），否则一次网络抖动就等于当天不交易。
            self.last = {"ok": False, "error": f"{type(e).__name__}: {e}",
                         "ts": time.time()}
            self._log(f"[auto] {self.mode} 执行抛错：{self.last['error']}")
            return
        elapsed = round(time.time() - t0, 2)
        self.last = {"ok": True, "ts": time.time(), "elapsed": elapsed, "result": res}
        # **成功提交之后**才写幂等账。注意这里记的是"已提交"而不是"已成交"：
        # 成交与否由引擎的风控与交易所决定，而重复提交的代价是实打实的双倍下单。
        st = self._read_state()
        st.update({"last_run_day": _today(), "last_run_at": _now_hhmm(),
                   "last_ts": time.time(), "last_elapsed": elapsed})
        self._write_state(st)
        self._log(f"[auto] {self.mode} 已提交（{elapsed}s）")


class Registry:
    """所有账户的调度器。控制台启动时按开关重建（自恢复）。"""

    def __init__(self) -> None:
        self._scheds: Dict[str, Scheduler] = {}
        self._lock = threading.RLock()

    def ensure(self, mode: str, runner: Callable[[str], dict], *,
               state_path: Callable[[], str],
               is_enabled: Callable[[], bool],
               log: Optional[Callable[[str], None]] = None) -> Scheduler:
        with self._lock:
            s = self._scheds.get(mode)
            if s is None:
                s = Scheduler(mode, runner, state_path=state_path,
                              is_enabled=is_enabled, log=log)
                self._scheds[mode] = s
            if not s.is_alive():
                s.start()
            return s

    def get(self, mode: str) -> Optional[Scheduler]:
        return self._scheds.get(mode)

    def status(self) -> List[dict]:
        out = []
        for m, s in sorted(self._scheds.items()):
            out.append({"mode": m, "at": s.at, "alive": s.is_alive(),
                        "last": s.last, "ran_today": s.already_ran_today()})
        return out
