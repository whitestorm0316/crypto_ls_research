"""A gate that was never run must not look like a gate that passed.

`optimize_report.acceptance_checks` states its own purpose in the module
docstring: to check the criteria explicitly "so a criterion that fails is
*reported as failing* rather than quietly omitted from the write-up."

The Monte-Carlo nulls were the exception.  That block was guarded by `if mc:`,
so a tag whose run did not include `--stages mc` produced **no rows at all** --
and the summary read 13/13.  Not hypothetical: `logs/research_v4_1d.log` shows
the accepted run's stage list, `mc` is not in it, and every archived tag on disk
is missing `22_montecarlo_summary.json`.  So the accepted configuration's
「13/13 通过」 never included the one test that can actually falsify it, and the
console could not tell the difference -- a missing gate and a passing gate
render identically.

These tests pin the *visibility* of the gap, not its verdict.  The rows come
back `pass=None`, so the gate count does not silently move between runs that did
and did not run the stage; what changes is that the gap is now on the page.
"""
from __future__ import annotations

import json
import os

import pytest

from crypto_ls_research.run import optimize_report as rep

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))


@pytest.fixture
def art(tmp_path, monkeypatch):
    """Redirect `acceptance_checks` at an empty artifact root."""
    monkeypatch.setattr(rep, "ART", str(tmp_path))
    return tmp_path


def _tables(art, tag: str) -> str:
    d = art / tag / "tables"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _mc_rows(rows: list) -> list:
    return [r for r in rows if str(r["criterion"]).startswith("MC null:")]


def test_a_missing_placebo_stage_is_reported_not_omitted(art):
    _tables(art, "t")
    rows = rep.acceptance_checks("t")
    mc = _mc_rows(rows)

    assert len(mc) == len(rep.MC_KINDS), (
        f"缺 22_montecarlo_summary.json 时只报了 {len(mc)} 条零假设行，"
        f"预期 {len(rep.MC_KINDS)} 条 —— 缺失的门禁又被静默省略了")
    for r in mc:
        assert r["pass"] is None, (
            "未评估的门禁不能算通过，也不能算失败：它既没跑过，就不能下任何判定")
        assert "未评估" in str(r["value"]), r
        assert "mc" in str(r["value"]), (
            "文案必须点名是哪个阶段没跑，否则用户不知道该怎么补")


def test_a_missing_placebo_stage_does_not_change_the_gate_count(art):
    """`pass=None` is informational, so the *denominator* is stable.

    If a missing stage produced `pass=False` the accepted tag would silently go
    from 13/13 to 13/16 the first time someone forgot `--stages mc` -- and a
    verdict that moves because a stage was skipped is worse than no verdict.
    """
    _tables(art, "t")
    rows = rep.acceptance_checks("t")
    assert not [r for r in rows if r["pass"] is False]
    assert all(r["pass"] is None for r in _mc_rows(rows))


def test_a_present_placebo_stage_is_a_real_pass_fail_gate(art):
    d = _tables(art, "t")
    payload = {k: {"sharpe": {"p_value": p, "real": 1.9, "percentile": 0.99}}
               for k, p in zip(rep.MC_KINDS, (0.005, 0.005, 0.0323))}
    with open(os.path.join(d, "22_montecarlo_summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(payload, f)

    mc = _mc_rows(rep.acceptance_checks("t"))
    assert len(mc) == len(rep.MC_KINDS)
    assert all(r["pass"] is True for r in mc)
    assert all("p=" in str(r["value"]) for r in mc)

    payload[rep.MC_KINDS[0]]["sharpe"]["p_value"] = 0.40
    with open(os.path.join(d, "22_montecarlo_summary.json"), "w",
              encoding="utf-8") as f:
        json.dump(payload, f)
    mc = {r["criterion"]: r["pass"] for r in _mc_rows(rep.acceptance_checks("t"))}
    assert mc[f"MC null: {rep.MC_KINDS[0]}"] is False
    assert mc[f"MC null: {rep.MC_KINDS[1]}"] is True


def test_the_expected_placebo_kinds_are_the_ones_the_stage_produces():
    """A rename on either side silently drops a gate.

    Checked as text rather than by importing `research`, which executes
    `set_tag("")` and pulls the whole backtest stack in at import time.
    """
    with open(os.path.join(ROOT, "crypto_ls_research/run/research.py"),
              encoding="utf-8") as f:
        src = f.read()
    for kind in rep.MC_KINDS:
        assert f'"{kind}"' in src, (
            f"acceptance_checks 期待的零假设类型 {kind!r} 在 stage_mc 里不存在 —— "
            "两边改名了，那条门禁会静默消失")
