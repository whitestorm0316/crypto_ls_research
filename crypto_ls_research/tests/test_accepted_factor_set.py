"""The accepted *factor set* has exactly one source, and it is the artifact.

`test_accepted_grid.py` pins the rebalance **grid** across four surfaces
(`DEFAULT_SIGNAL`, the CLI defaults, `spec.OPTIMAL_CLI`, the acceptance run) and
says why: three of them once carried their own literal, the daemon traded daily
while the console printed 「下次调仓 3 天后」, and nothing raised.

The factor set is the same class of configuration and had no equivalent guard.
`test_accepted_grid.py` does tie

    webapp.spec.OPTIMAL_OVERRIDES == execution.engine.DEFAULT_SIGNAL["overrides"]

so console and desk cannot drift apart.  What nothing tied them to was the third
surface:

    artifacts/<OPTIMAL_TAG>/tables/00_run_meta.json  ->  "factor_subset"

-- i.e. the factor set the evidence was actually produced with.  That is the link
that matters most.  A console and a desk agreeing on a factor set that the
acceptance run never measured looks exactly like a healthy page: the headline,
the walk-forward table and the gate list are all real numbers, they simply
describe a different book.  Same failure mode as the grid, one configuration key
over.

2026-09-29: the basis moved from the pruned two factors to all five.  So these
assertions are about **agreement**, not about the value -- they must stay green
when the basis is deliberately re-designated and go red when one surface drifts.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from webapp import spec

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))


def _accepted_subset() -> list:
    return list(spec.OPTIMAL_OVERRIDES["factors.subset"])


def _run_meta(tag: str) -> dict:
    path = os.path.join(ROOT, "artifacts", tag, "tables", "00_run_meta.json")
    if not os.path.exists(path):
        pytest.skip(f"artifacts/{tag}/tables/00_run_meta.json 不在盘上")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# the three surfaces
# ---------------------------------------------------------------------------
def test_the_console_and_the_desk_name_the_same_factor_set():
    """The pair `test_accepted_grid.py` already pins -- restated here so this
    file is readable on its own.  It is the *precondition* for the next test:
    if these two already disagree, comparing either of them to the artifact
    tells you nothing about the other."""
    from crypto_ls_research.execution.engine import DEFAULT_SIGNAL

    assert _accepted_subset() == list(
        DEFAULT_SIGNAL["overrides"]["factors.subset"])


def test_the_accepted_factor_set_is_the_one_the_acceptance_run_used():
    """The link that was missing.

    `00_run_meta.json` is written by the run itself and is the only place that
    says what the headline / walk-forward / gate numbers were measured on.  If
    the console's 「已验收最优」 names a different subset, every number on the
    page is real and describes a book nobody measured.
    """
    from crypto_ls_research.execution.engine import DEFAULT_SIGNAL

    meta = _run_meta(spec.OPTIMAL_TAG)
    used = list(meta["factor_subset"])

    assert sorted(used) == sorted(_accepted_subset()), (
        f"控制台「已验收最优」的因子子集 {_accepted_subset()} 与 "
        f"artifacts/{spec.OPTIMAL_TAG}/ 实测的 {used} 不一致 —— "
        "页面上的每个数字都是真的，但它描述的是另一本书")
    assert sorted(used) == sorted(
        DEFAULT_SIGNAL["overrides"]["factors.subset"]), (
        "交易台的 DEFAULT_SIGNAL 与验收产物不是同一个因子子集")


def test_the_accepted_factor_set_only_names_factors_that_exist():
    """A typo here is silent: the composite score just ignores the unknown key,
    and the book narrows without anything saying so."""
    unknown = [f for f in _accepted_subset() if f not in spec.FACTOR_NAMES]
    assert not unknown, f"验收口径里有不存在的因子名：{unknown}"


def _definition_block(src: str, marker: str, width: int) -> str:
    """The source text of one definition, from `marker` for `width` chars.

    A whole-file "does not contain the factor names" check is useless here:
    `spec.py` legitimately enumerates them in `FACTOR_NAMES` / `FACTOR_LABEL`
    and in the **control** presets (four factors, three, two).  What must not be
    re-typed is the *accepted* definition, so that is what gets sliced out.
    """
    i = src.index(marker)
    return src[i:i + width]


def test_the_accepted_factor_set_has_exactly_one_literal():
    """The grid has `ACCEPTED_REBALANCE_DAYS`; the factor set needs the same.

    `test_accepted_grid.py` pins `OPTIMAL_OVERRIDES == DEFAULT_SIGNAL["overrides"]`
    and `test_these_files_derive_the_grid_instead_of_re_typing_it` pins the *text*
    of the files that must name the grid constant.  The factor set is the same
    hazard and was, until this test, two independent literals that happened to
    agree -- i.e. one edit away from the exact defect the grid guard exists for.
    """
    from crypto_ls_research.config.settings import ACCEPTED_FACTORS
    from crypto_ls_research.execution.engine import DEFAULT_SIGNAL

    assert list(ACCEPTED_FACTORS) == _accepted_subset(), (
        "OPTIMAL_OVERRIDES 没有从 ACCEPTED_FACTORS 派生")
    assert list(ACCEPTED_FACTORS) == list(
        DEFAULT_SIGNAL["overrides"]["factors.subset"]), (
        "DEFAULT_SIGNAL 没有从 ACCEPTED_FACTORS 派生")

    for rel, marker, width in (
            ("webapp/spec.py", "OPTIMAL_OVERRIDES = {", 300),
            ("crypto_ls_research/execution/engine.py", "DEFAULT_SIGNAL: dict = {", 700)):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            block = _definition_block(f.read(), marker, width)
        assert "ACCEPTED_FACTORS" in block, (
            f"{rel} 的验收定义没有引用 ACCEPTED_FACTORS —— 因子集又变成了字面量")
        for name in _accepted_subset():
            assert f'"{name}"' not in block, (
                f"{rel} 的验收定义里写死了因子名 {name!r}；"
                "它只应来自 config/settings.py::ACCEPTED_FACTORS")


def test_the_console_opens_on_a_preset_that_is_the_accepted_factor_set():
    """`app.js` selects a preset by id on boot.  If that id is renamed away the
    page silently falls back to whatever the schema default is, which is a
    *different* factor set -- and the only symptom is that the 「偏离最优」 pill
    reads 0 while the book is not the accepted one."""
    with open(os.path.join(ROOT, "webapp/static/app.js"), encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"OPTIMAL_PRESET_ID\s*=\s*'([^']+)'", src)
    assert m, "app.js 里找不到 OPTIMAL_PRESET_ID"

    pid = m.group(1)
    by_id = {p["id"]: p for p in spec.PRESETS}
    assert pid in by_id, f"app.js 默认选中的预置 {pid!r} 不在 spec.PRESETS 里"
    assert list(by_id[pid]["overrides"]["factors.subset"]) == _accepted_subset(), (
        f"预置 {pid!r} 自称「已验收最优」，但它的因子子集与 OPTIMAL_OVERRIDES 不同")


def test_some_preset_can_express_the_accepted_factor_set():
    """A configuration with no button is a configuration nobody can reproduce
    from the page.  This is how the five-factor set was previously unreachable:
    the schema exposed the key but every preset was a *narrower* subset."""
    subsets = [tuple(p["overrides"].get("factors.subset", ()))
               for p in spec.PRESETS]
    assert tuple(_accepted_subset()) in subsets, (
        f"没有任何预置能表达验收口径 {_accepted_subset()}")


# ---------------------------------------------------------------------------
# the reversal disclosure must survive the promotion -- reframed, not deleted
# ---------------------------------------------------------------------------
def test_the_reversal_note_does_not_call_the_accepted_config_rejected():
    """`rev_short` is now *in* the accepted factor set.

    `check_traps` documents itself as "warnings for configurations this project
    has already rejected", and the reversal entry used to be exactly that --
    `sev: "high"`, titled 「稳健性检验否决它」.  Once the accepted set contains
    `rev_short`, that entry fires on the console's own optimal configuration and
    tells the user they have configured something falsified.  The verdict has to
    change shape with the basis; it must not be deleted.
    """
    warns = spec.check_traps(dict(spec.OPTIMAL_OVERRIDES), {})
    rev = [w for w in warns if w.get("key") == "factors.rev_short"]

    assert not [w for w in rev if w.get("sev") == "high"], (
        "验收口径自己带着 rev_short，控制台却仍用 high 级别警告说它被否决了")


def test_the_reversal_disclosure_still_states_the_robustness_facts():
    """The point of the reframe is that the *evidence* stays on the page.

    `rev_short` is now part of the accepted factor set, so the entry had to stop
    calling the user's own optimum "falsified".  Deleting the inconvenient numbers
    is the cheap way to make the previous test green, so this one pins the facts
    that make the disclosure worth anything.  Every one of them is readable from
    the accepted run's own tables:

    * `1.762 → 1.581` -- the 3-day grid reverses the ordering
      (`scripts/exp_factor_set_grid.py`, `artifacts/factor_set_grid/`);
    * `2.029 vs 1.957` -- walk-forward test mean is not better while the train
      mean is (`34b_neutralisation_folds.csv`);
    * `2.740 → 2.172` -- the **locked 2026 slice**, which was never used for any
      selection, is *worse* (`18c_locked_oos.csv`, both tags);
    * `+0.0225/t=3.10 → +0.0051/t=0.70` -- the composite score's rank IC at the
      horizon actually traded loses significance (`12_rank_ic_by_factor.csv`);
    * `0.0050` -- the placebo nulls, i.e. the part that came out *well*
      (`22_montecarlo_summary.json`).

    A disclosure that only lists the bad news would be as misleading as one that
    only lists the good, so the nulls are asserted here too.

    Queried with **all five** factors rather than with the accepted set: before
    the basis moved, the accepted set did not contain `rev_short` and this
    assertion would have been vacuous -- passing because there was nothing to
    check, which is the failure mode this file exists to prevent.
    """
    warns = spec.check_traps({"factors.subset": list(spec.FACTOR_NAMES)}, {})
    body = " ".join(w.get("body", "") for w in warns
                    if w.get("key") == "factors.rev_short")
    assert body, "反转腿的说明整条消失了 —— 这是删证据，不是改口径"

    for fact in ("1.762", "1.581",          # 3-day grid: the ordering reverses
                 "2.029", "1.957",          # walk-forward test mean: not better
                 "2.740", "2.172",          # locked 2026 slice: worse
                 "0.0225", "0.0051",        # traded-horizon IC: loses significance
                 "0.0050"):                 # placebo nulls: the part that holds
        assert fact in body, f"反转腿说明里少了事实 {fact}"
