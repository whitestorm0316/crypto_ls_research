"""`--end` 的默认值必须跟「今天」走，不能是某个冻结的日期。

为什么值得一条守卫
------------------
`2026-09-26` 曾是**验收窗口**的上界，被同时写成了 `run.research` 与
`data.download` 的 `--end` 默认值，而且它是**排他上界**。新机器上照默认跑：

    data.download  -> 缓存停在 2026-09-26
    run.research   -> 窗口停在 2026-09-26
    两者都不报错    -> "回测就是少了点 bar"

2026-10-19 实测：少 23 天 / 23 次调仓，headline 从 2.014 掉到 1.696，
日志里一个错误都没有。冻结的常量是**纪录**，冻结的默认值是**陷阱**。

断言方式
--------
直接问 argparse 本身，而不是 grep 源码：`main()` 里 `add_argument` 一被调用就
把 `--end` 的 default 抓下来，随后 `--help` 让进程正常退出（不会跑回测、不会
联网）。这样"默认值"是被**执行**出来的，不是被文本匹配出来的 —— 文本守卫在
本仓库已经假绿过一次（按行解析续行的 shell，参数一个没抓到）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import pytest

from crypto_ls_research.config.settings import default_end
from crypto_ls_research.data import download as dl_mod
from crypto_ls_research.run import research as rs_mod


def _capture_end_default(module, monkeypatch) -> str:
    """跑 module.main() 到「建完 parser」为止，返回 --end 的 default。"""
    got = {}
    real = argparse.ArgumentParser.add_argument

    def spy(self, *a, **kw):
        if a and a[0] == "--end":
            got["default"] = kw.get("default")
        return real(self, *a, **kw)

    monkeypatch.setattr(argparse.ArgumentParser, "add_argument", spy)
    monkeypatch.setattr(sys, "argv", ["prog", "--help"])
    with pytest.raises(SystemExit):
        module.main()
    # 前提断言：真的抓到了 --end，否则下面的比较是在比 None（假绿）
    assert "default" in got, f"{module.__name__} 没有 --end 参数，守卫抓空了"
    return got["default"]


def test_default_end_is_tomorrow_utc():
    assert default_end(date(2026, 10, 19)) == "2026-10-20"
    assert default_end(date(2026, 12, 31)) == "2027-01-01"   # 跨年


def test_default_end_is_not_frozen():
    """跨两年都要变：写死的字面量不可能同时等于这两个值。"""
    assert default_end(date(2026, 10, 19)) != default_end(date(2030, 10, 19))


@pytest.mark.parametrize("module", [rs_mod, dl_mod], ids=["research", "download"])
def test_cli_end_default_tracks_today(module, monkeypatch):
    assert _capture_end_default(module, monkeypatch) == default_end()
