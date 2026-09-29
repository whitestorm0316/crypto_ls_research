"""The funding criterion, and the failure that must stay visible.

Why this file exists
--------------------
`acceptance_checks` decides whether a research tag is "accepted", and it had no
test at all.  That was survivable until the funding term changed sign under a
data rebuild: the recorded v3 acceptance said `资金费 P&L = +2.06%（净收取）`, the
rebuilt artifacts say `-3.00%` (a net payer), and the obvious "fix" is to delete
or invert the criterion so the console goes green again.

That is the one thing these tests forbid.  The roadmap's criterion really does
fail on the current data, so it must keep reporting as a failure; what must
change is that the *reason* travels with it.  The funding term's sign is not a
property of the strategy -- it is a small residual whose sign is decided by where
the sample ends and by one or two instruments -- and that is now disclosed in the
same row list, as information rather than as a gate.

So the two load-bearing properties are:
  1. a net-payer funding term reports the roadmap criterion as **FAIL**, and
  2. the instability is disclosed **next to** it, without altering the gate count.
"""
from __future__ import annotations

import json
import pickle
import types

import numpy as np
import pandas as pd
import pytest

from crypto_ls_research.run import optimize_report as rep

TAG = "vtest"


def _bars(per_year: dict) -> pd.DataFrame:
    """Hourly bars whose funding column sums to `per_year` per calendar year.

    `net_ret` satisfies the ledger identity exactly, so the identity row is not
    what this file is testing -- a failure there would be a different bug.
    """
    idx = pd.date_range("2023-01-01", "2026-01-01", freq="h", tz="UTC", inclusive="left")
    n = len(idx)
    fund = np.zeros(n)
    for y, total in per_year.items():
        m = idx.year == y
        fund[m] = total / int(m.sum())
    gross = np.full(n, 0.0001)
    fee = np.full(n, 0.00002)
    spread = np.full(n, 0.00001)
    impact = np.full(n, 0.000005)
    return pd.DataFrame({
        "net_ret": gross - (fee + spread + impact) + fund,
        "gross_ret": gross, "fee": fee, "spread": spread, "impact": impact,
        "funding": fund,
    }, index=idx)


def _tag(tmp_path, monkeypatch, *, per_year, scenarios, funding_total=None,
         name_fund=None) -> None:
    art = tmp_path / "artifacts" / TAG
    (art / "tables").mkdir(parents=True)
    monkeypatch.setattr(rep, "ART", str(tmp_path / "artifacts"))

    bars = _bars(per_year)
    # The real run has `headline["Funding P&L"] == bars["funding"].sum()` exactly
    # (both come from the same column).  A fixture where they disagree would let a
    # disclosure that reads one while the gate reads the other look correct.
    total = float(bars["funding"].sum()) if funding_total is None else funding_total
    assert abs(total - float(bars["funding"].sum())) < 1e-12, \
        "fixture is inconsistent: headline funding != bars funding"

    (art / "tables" / "01_headline_metrics.json").write_text(
        json.dumps({"Funding P&L (total, frac)": total,
                    "Trading Cost (total, frac)": 0.1186}), encoding="utf-8")
    (art / "tables" / "01b_cost_scenarios.csv").write_text(
        pd.DataFrame({"scenario": list(scenarios), "CAGR": list(scenarios.values())}
                     ).to_csv(index=False), encoding="utf-8")

    res = types.SimpleNamespace(
        bars=bars,
        name_fund=np.asarray(name_fund if name_fund is not None
                             else [[0.01, -0.004, 0.001]] * 4, dtype="float64"),
    )
    with open(art / "baseline.pkl", "wb") as f:
        pickle.dump({"result": res}, f)


PAYER = dict(gross=0.2442, no_trading_cost=0.2377, no_funding=0.2188, net=0.2124)
RECEIVER = dict(gross=0.2000, no_trading_cost=0.2050, no_funding=0.1900, net=0.1950)
# early window a credit, late window a cost -> the whole-sample sign is negative
FLIPPING = {2023: +0.036, 2024: +0.004, 2025: -0.070}


def _row(rows, needle):
    hit = [r for r in rows if needle in str(r["criterion"])]
    assert len(hit) == 1, f"expected exactly one row matching {needle!r}, got {len(hit)}"
    return hit[0]


# ---------------------------------------------------------------------------
def test_a_net_payer_reports_the_roadmap_criterion_as_failing(tmp_path, monkeypatch):
    """The whole point: the failure must not be deleted to make the page green."""
    _tag(tmp_path, monkeypatch, per_year=FLIPPING,
         scenarios=PAYER)
    row = _row(rep.acceptance_checks(TAG), "is a credit")
    assert row["pass"] is False


def test_a_net_receiver_still_passes_the_roadmap_criterion(tmp_path, monkeypatch):
    """Paired: a gate that always fails is just a different way of being useless."""
    _tag(tmp_path, monkeypatch, per_year={2023: +0.01, 2024: +0.005, 2025: +0.006},
         scenarios=RECEIVER)
    row = _row(rep.acceptance_checks(TAG), "is a credit")
    assert row["pass"] is True


def test_the_sign_instability_is_disclosed_next_to_the_failure(tmp_path, monkeypatch):
    """A bare FAIL sends the next person to re-derive the same afternoon of work.

    The disclosure has to carry the three facts that make the sign untrustworthy:
    the yearly signs, how small the net is relative to the gross, and that one
    instrument can outweigh the whole net.  Expected values are derived from the
    fixture, never typed in -- a hard-coded 0.142 here would just invite someone
    to edit the assertion until it agreed with the code.
    """
    per_inst = [[0.09, -0.02, -0.01]] * 4
    _tag(tmp_path, monkeypatch, per_year=FLIPPING,
         scenarios=PAYER, name_fund=per_inst)
    rows = rep.acceptance_checks(TAG)

    bars = _bars(FLIPPING)
    fr = bars["funding"]
    want_ratio = abs(fr.sum()) / fr.abs().sum()
    want_biggest = float(np.abs(np.asarray(per_inst).sum(axis=0)).max())

    disc = _row(rows, "sign is NOT stable")
    assert disc["pass"] is None                      # information, not a gate
    for y, v in FLIPPING.items():                    # every per-year sign is shown
        assert f"{y}:{v:+.1%}" in disc["value"]
    assert f"net/gross = {want_ratio:.3f}" in disc["value"]
    assert f"单币最大 |贡献| = {want_biggest:.4f}" in disc["value"]

    split = _row(rows, "sample split decides the sign")
    assert split["pass"] is None
    early = sum(v for y, v in FLIPPING.items() if y <= 2024)
    late = sum(v for y, v in FLIPPING.items() if y >= 2025)
    assert f"2021-2024 = {early:+.4f}" in split["value"]
    assert f"2025-now = {late:+.4f}" in split["value"]


def test_the_disclosure_does_not_change_the_gate_count(tmp_path, monkeypatch):
    """Informational rows must not inflate `n_pass / n_gate`.

    The console's "完整需 16 项" bookkeeping counts gates only, so a disclosure
    that quietly became a gate would shift the count -- and would look like a
    *better* result, which is exactly why nobody would notice.

    The assertion is deliberately not "gates and info partition the rows": that
    is true by construction and reddens under nothing.  It compares the gate list
    against the rows that *declare themselves* non-gates.

    There are two legitimate reasons for `pass=None`, and both must say so:

    * **`(informational)`** -- the criterion *was* measured, and is deliberately
      not gated (the funding-sign disclosures);
    * **`未评估`** -- the criterion was **never evaluated**, because the stage
      that produces it did not run (the placebo nulls, when `--stages mc` is
      missing).  Before this category existed those rows were simply not emitted
      at all and the summary read a clean 13/13 -- see
      `tests/test_acceptance_completeness.py`.

    An *undeclared* `pass=None` row still reddens here, which is the point.
    """
    _tag(tmp_path, monkeypatch, per_year=FLIPPING,
         scenarios=PAYER)
    rows = rep.acceptance_checks(TAG)

    gates = [r for r in rows if r["pass"] is not None]
    labelled_info = [r for r in rows if "(informational)" in str(r["criterion"])]
    unassessed = [r for r in rows if "未评估" in str(r["value"])]
    assert len(labelled_info) == 2, "expected exactly two disclosure rows"
    assert len(gates) == len(rows) - len(labelled_info) - len(unassessed), (
        "有 pass=None 的行没有声明自己为什么不是门禁 —— 门禁数会被悄悄改掉")
    for r in labelled_info + unassessed:
        assert r["pass"] is None, \
            f"{r['criterion']} became a gate; the 16-item count would shift"


def test_the_ledger_identity_row_still_reads_the_funding_column(tmp_path, monkeypatch):
    """Guards the row that actually catches the double-negation bug.

    If `funding` were subtracted instead of added, this residual would be
    `2·|funding|`, not 1e-19 -- so this is the criterion that protects the
    convention, and it must keep existing alongside the sign discussion.
    """
    _tag(tmp_path, monkeypatch, per_year=FLIPPING,
         scenarios=PAYER)
    row = _row(rep.acceptance_checks(TAG), "ledger identity")
    assert row["pass"] is True
    # Derive the tolerance from the fixture rather than pinning an exponent: this
    # synthetic ledger satisfies the identity exactly, the real one at 4e-19.
    resid = float(row["value"].split("= ")[1].split(" ")[0])
    assert resid < 1e-12


@pytest.mark.parametrize("years,expect_early_credit", [
    ({2023: +0.036, 2024: +0.004, 2025: -0.029}, True),
    ({2023: -0.030, 2024: -0.010, 2025: +0.040}, False),
])
def test_the_split_row_reports_whichever_way_the_data_actually_goes(
        tmp_path, monkeypatch, years, expect_early_credit):
    """Two-sided: the row reports the split, it does not assume the direction."""
    _tag(tmp_path, monkeypatch, funding_total=sum(years.values()),
         per_year=years, scenarios=PAYER)
    value = _row(rep.acceptance_checks(TAG), "sample split decides the sign")["value"]
    early = float(value.split("2021-2024 = ")[1].split(" ")[0])
    assert (early >= 0) is expect_early_credit
