"""Run the test suite **without pytest's session machinery**.

Why this exists
---------------
`pytest` deletes its own temp root (`%TEMP%\\pytest-of-<user>\\garbage-*`) on
every single run.  On this machine that trips the host's file-deletion guard,
which raises a confirmation prompt and, once refused, kills the command and
stalls the session -- so `pytest` cannot be run here at all, in any form
(`--basetemp`, `-p no:cacheprovider`, ... all still clean that directory).

This runner calls the test functions directly instead.  It allocates scratch
directories under `.wb_tmp/manual/` and **never deletes anything**.

Supported: `tmp_path`, `monkeypatch`, module-level fixtures (including generator
fixtures and `autouse=True`), `skipif`, `xfail` / `xfail(strict)`.
Anything it cannot emulate (parametrised fixtures, `capsys`, `request`, ...) is
reported as `skip`, never silently passed.

Usage
-----
    python scripts/run_tests.py                       # the whole suite
    python scripts/run_tests.py crypto_ls_research/tests/test_navhist.py
    python scripts/run_tests.py crypto_ls_research/tests/test_*.py -- splice

Baseline (2026-10-03): 514 ok / 2 FAIL / 31 skip -- the two failures are the
pre-existing `test_failure_attribution` W1-window pair, unrelated to anything
else.
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import pathlib
import sys
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402  (approx/raises/marks only -- no session)
from _pytest.monkeypatch import MonkeyPatch  # noqa: E402
from _pytest.outcomes import Skipped, XFailed  # noqa: E402

TESTS = ROOT / "crypto_ls_research" / "tests"
SCRATCH = ROOT / ".wb_tmp" / "manual"
_SEQ = [0]


def _fresh(tag: str) -> pathlib.Path:
    _SEQ[0] += 1
    d = SCRATCH / f"{tag}-{_SEQ[0]:04d}-{int(time.time() * 1000) % 100000}"
    d.mkdir(parents=True, exist_ok=True)          # created, never removed
    return d


def _load(path: pathlib.Path):
    """Import as part of its package: several test modules use relative imports
    (`from .conftest import ...`) which only resolve under a package."""
    rel = path.resolve().relative_to(ROOT)
    dotted = ".".join(rel.with_suffix("").parts)
    if (ROOT / rel.parent / "__init__.py").exists():
        return importlib.import_module(dotted)
    spec = importlib.util.spec_from_file_location(path.stem + "_direct", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_fixture(obj) -> bool:
    # pytest <=8 tags the function with `_pytestfixturefunction`; pytest 9 wraps
    # it in a `FixtureFunctionDefinition` exposing `_fixture_function`.
    return (hasattr(obj, "_fixture_function")
            or hasattr(obj, "_pytestfixturefunction"))


def _unwrap(fn):
    return getattr(fn, "_fixture_function", fn)


def _resolve(mod, name: str, cache: dict, finals: list):
    """Build one fixture value; falls back to a module-level fixture function."""
    if name in cache:
        return cache[name]
    if name == "tmp_path":
        d = _fresh("p")
        cache[name] = d
        return d
    if name == "monkeypatch":
        mp = MonkeyPatch()
        cache[name] = mp
        finals.append(mp.undo)
        return mp
    fn = getattr(mod, name, None)
    if fn is None or not _is_fixture(fn):
        raise KeyError(name)
    raw = _unwrap(fn)
    kwargs = {p: _resolve(mod, p, cache, finals)
              for p in inspect.signature(raw).parameters if p != "request"}
    val = raw(**kwargs)
    if inspect.isgenerator(val):
        gen = val
        val = next(gen)
        finals.append(lambda g=gen: next(g, None))
    cache[name] = val
    return val


def _autouse(mod) -> list:
    """Fixture names the module marks `autouse=True`.

    Skipping these is not cosmetic: `test_navhist` uses one to drop
    `navhist._PROBE`'s 90-second verdict cache between tests, and without it the
    *order* of the file decides whether OKX counts as reachable -- exactly the
    flake that fixture exists to prevent.
    """
    out = []
    for name, obj in vars(mod).items():
        if not _is_fixture(obj):
            continue
        marker = (getattr(obj, "_fixture_function_marker", None)
                  or getattr(obj, "_pytestfixturefunction", None))
        if marker is not None and getattr(marker, "autouse", False):
            out.append(name)
    return out


def _marks(fn):
    return {m.name: m for m in getattr(fn, "pytestmark", [])}


def _run_module(path: pathlib.Path, flt: list, tally: dict) -> None:
    mod = _load(path)
    names = [n for n, f in vars(mod).items()
             if n.startswith("test_") and inspect.isfunction(f)
             and f.__module__ == mod.__name__]
    if flt:
        names = [n for n in names if any(a in n for a in flt)]
    autouse = _autouse(mod)

    print(f"\n### {path.name}  ({len(names)} tests)")
    for name in names:
        fn = getattr(mod, name)
        marks = _marks(fn)
        if inspect.iscoroutinefunction(fn):
            print(f"skip  {name}: coroutine")
            tally["skip"] += 1
            continue
        if "skipif" in marks:
            try:
                args = marks["skipif"].args
                if args and bool(args[0]):
                    print(f"skip  {name}: skipif")
                    tally["skip"] += 1
                    continue
            except Exception:                                  # noqa: BLE001
                pass

        cache, finals = {}, []
        try:
            for a in autouse:                    # resolve before the test's own
                _resolve(mod, a, cache, finals)
            kwargs = {p: _resolve(mod, p, cache, finals)
                      for p in inspect.signature(fn).parameters}
        except KeyError as e:
            print(f"skip  {name}: unresolved fixture {e}")
            tally["skip"] += 1
            for f in reversed(finals):
                f()
            continue
        except (Skipped, XFailed) as e:
            print(f"skip  {name}: {e}")
            tally["skip"] += 1
            for f in reversed(finals):
                f()
            continue

        want_xfail = "xfail" in marks
        strict = bool(getattr(marks.get("xfail"), "kwargs", {}).get("strict"))
        try:
            fn(**kwargs)
            if want_xfail and strict:
                print(f"FAIL  {name}: XPASS(strict) -- 缺陷已修，测试该转正了")
                tally["fail"] += 1
                tally["detail"].append((path.name, name, "XPASS(strict)"))
            elif want_xfail:
                print(f"xpass {name}")
                tally["xpass"] += 1
            else:
                print(f"ok    {name}")
                tally["ok"] += 1
        except Skipped as e:
            print(f"skip  {name}: {e}")
            tally["skip"] += 1
        except XFailed as e:
            print(f"xfail {name}: {e}")
            tally["xfail"] += 1
        except Exception as e:                                 # noqa: BLE001
            if want_xfail:
                print(f"xfail {name}: {type(e).__name__}")
                tally["xfail"] += 1
            else:
                print(f"FAIL  {name}: {type(e).__name__}: {e}")
                tally["fail"] += 1
                tally["detail"].append((path.name, name, e))
                traceback.print_exc(limit=8)
        finally:
            for f in reversed(finals):
                try:
                    f()
                except Exception:                              # noqa: BLE001
                    pass


def main(argv: list) -> int:
    flt = argv[argv.index("--") + 1:] if "--" in argv else []
    files = [a for a in argv if a.endswith(".py")]
    if not files:
        files = sorted(str(p.relative_to(ROOT)) for p in TESTS.glob("test_*.py"))
    SCRATCH.mkdir(parents=True, exist_ok=True)
    tally = {"ok": 0, "fail": 0, "skip": 0, "xfail": 0, "xpass": 0, "detail": []}
    for f in files:
        p = pathlib.Path(f)
        _run_module(p if p.is_absolute() else ROOT / p, flt, tally)
    print("\n" + "=" * 70)
    print(f"ok {tally['ok']} / FAIL {tally['fail']} / xfail {tally['xfail']} / "
          f"xpass {tally['xpass']} / skip {tally['skip']}")
    for fname, tname, e in tally["detail"]:
        print(f"  FAIL {fname}::{tname}: {e}")
    print("没有任何文件被删除；scratch 留在", SCRATCH)
    return 1 if tally["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
