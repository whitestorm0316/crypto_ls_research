"""`scripts/init_linux.sh` 与本仓库真实 CLI 的一致性守卫。

为什么需要这份测试
------------------
新机器初始化只有一个入口（那份脚本），而它调的是三个**模块的 argparse**。
argparse 的参数名是**运行时**才校验的 —— 把 `--only-candles` 改名、或把
`--bars` 改成位置参数，脚本**照样能写、照样能提交**，直到有人在全新机器上
跑到第 2 步才炸。而那时他已经花了半小时。

所以这里把「脚本里出现的每个 `python -m ... ` 调用」都**真的**跑一次
`--help`，确认那个模块存在、且参数名被接受。

⚠️ **不靠手抄参数名**：从脚本里**正则抓**命令，和 Q55 那条
"从 index.html 抓 src/href" 是同一个道理 —— 手抄的那份清单会漂移。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))
SCRIPT = os.path.join(ROOT, "scripts", "init_linux.sh")


def _script_text() -> str:
    assert os.path.exists(SCRIPT), f"缺少 {SCRIPT}"
    return open(SCRIPT, encoding="utf-8").read()


def _module_invocations() -> list[list[str]]:
    """从脚本里抓 `-m <module>` 后面跟的全部参数。

    ⚠️ **必须先拼回续行**。命令长这样：

        "$PY" -m crypto_ls_research.data.download \\
              --bars $BARS --end "$END" --rate 11 --workers 12

    第一版**逐行**解析，于是这条只抓到模块名、参数一个都没拿到 ⇒
    往续行里注入 `--bogus-flag` 或改一个 flag 名，测试**全绿**（变异测试当场
    抓出来的漏网，D1/D5 两条）。拼行之后才真正对账。
    """
    raw_lines = _script_text().splitlines()
    # 先把反斜杠续行拼成逻辑行
    logical: list[str] = []
    buf = ""
    for ln in raw_lines:
        stripped = ln.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        logical.append(buf + ln)
        buf = ""
    if buf:
        logical.append(buf)

    out: list[list[str]] = []
    for line in logical:
        s = line.strip()
        # 只看真实调用行（`$PY`/`"$PY"`/python3 紧跟 -m）
        if not re.match(r'^("?\$\{?PY\}?"?|python3?)\s+-m\s+', s):
            continue
        s = s.split("||")[0].split("&&")[0].strip()
        toks = s.replace('"$PY"', "PY").replace("$PY", "PY").split()
        try:
            i = toks.index("-m")
        except ValueError:
            continue
        mod = toks[i + 1]
        args = [t for t in toks[i + 2:] if not t.startswith("#")]
        # 去掉 shell 重定向/管道尾巴
        args = [a for a in args if a not in (">", ">>", "|", "2>&1")]
        out.append([mod, *args])
    return out


def test_the_script_actually_invokes_the_pipeline():
    """前提：脚本里确实有那些调用（否则下面的断言全是空转）。"""
    mods = {m for m, *_ in _module_invocations()}
    for want in ("crypto_ls_research.data.download",
                 "crypto_ls_research.data.asset_class",
                 "crypto_ls_research.data.funding_build"):
        assert want in mods, (
            f"脚本里没找到 `-m {want}` —— 要么改了实现，要么正则失效了。"
            f"实抓到：{sorted(mods)}")


@pytest.mark.parametrize("mod", [
    "crypto_ls_research.data.download",
    "crypto_ls_research.data.asset_class",
    "crypto_ls_research.data.funding_build",
])
def test_each_invoked_module_exists(mod):
    """模块能被 `-m` 跑起来（catch 拼写错误 / 文件被移走）。"""
    r = subprocess.run([sys.executable, "-m", mod, "--help"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, (
        f"`python -m {mod} --help` 失败（{r.returncode}）：\n"
        f"{(r.stdout + r.stderr)[-600:]}")
    assert "usage" in (r.stdout + r.stderr).lower()


def test_every_flag_the_script_passes_is_accepted():
    """🔴 **脚本传的每个 flag，argparse 必须真的认。**

    这条是核心：argparse 的参数名只在运行时校验，改名不会有任何静态报错。
    这里对每条调用跑一次 `--help`，把它打印出的 flag 集合与脚本用的集合对账。
    """
    problems = []
    for inv in _module_invocations():
        mod, args = inv[0], inv[1:]
        flags = {a.split("=")[0] for a in args if a.startswith("--")}
        if not flags:
            continue
        r = subprocess.run([sys.executable, "-m", mod, "--help"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        helptext = r.stdout + r.stderr
        missing = sorted(f for f in flags if f not in helptext)
        if missing:
            problems.append(f"  -m {mod}: 脚本用了但 argparse 不认 {missing}")
    assert not problems, (
        "脚本与本仓库 CLI 不一致（新机器上会跑到一半才炸）：\n"
        + "\n".join(problems))


def test_the_script_does_not_use_the_stale_default_end():
    """🔴 脚本**不许**依赖 `data.download` 的 `--end` 默认值。

    那个默认是硬编码的 `2026-09-26`，而它是**排他上界**。新机器上照默认跑
    会静静少二十来天 —— 不报错，后续回测就在旧数据上出结论。
    脚本必须自己算一个"明天 UTC"显式传进去。
    """
    src = _script_text()
    assert "--end" in src, "脚本没有传 --end —— 会用到那个陈旧默认值"
    assert re.search(r'--end\s+"\$END"', src), (
        "脚本里的 --end 不是一个变量（应当是 `--end \"$END\"`，由 date 算出来）")
    assert "date -u" in src, "脚本没有用 date 计算 END"
    assert "tomorrow" in src or "-v+1d" in src, (
        "END 应当是**明天**（排他上界要越过今天）；没看到 tomorrow/+1d")


def test_the_script_documents_the_survivorship_caveat():
    """候选池来自今天的实时挂单 ⇒ 跨机器不可比。这条必须写在脚本里。

    ⚠️ 断言的是**那句话在不在**，因为它是操作者唯一会读到的地方 ——
    如果他只跑脚本、没看 DEPLOY_LINUX.md，就不会知道两台的数字不能直接比。
    """
    src = _script_text()
    assert "候选池" in src and ("survivorship" in src.lower()
                                or "实时挂单" in src), (
        "脚本里没提候选池是从今天的实时挂单建的 ⇒ 跨机器结论不可比")
