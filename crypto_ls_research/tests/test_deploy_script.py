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


# ---------------------------------------------------------------------------
# 代理（2026-10-16：新机器上第一次跑就撞了 `Errno 111 Connection refused`）
# ---------------------------------------------------------------------------
def test_script_exports_the_proxy_variable():
    """🔴 `init_linux.sh` 必须 `export OKX_PROXY`。

    三个客户端（`okx_client` / `binance_client` / `okx_private`）都从**这一个**
    环境变量读代理，默认 `http://127.0.0.1:7897`。换台机器代理端口不同（常见
    7890）就会在到达交易所之前被本机代理拒掉，报的是
    `URLError: [Errno 111] Connection refused` —— 那个错**看不出是代理问题**。

    ⚠️ 必须是 `export` 而不只是赋值：脚本是用子进程跑 `python -m ...` 的，
    没 export 的话子进程读不到（`DEFAULT_PROXY` 是**模块导入时**读的，
    所以还得在启动子进程**之前**就设好）。
    """
    src = _script_text()
    assert re.search(r"^\s*export\s+OKX_PROXY=", src, re.M), (
        "脚本没有 `export OKX_PROXY=...` —— 子进程读不到代理设置")


def test_script_has_a_proxy_preflight_before_downloading():
    """必须**先体检代理**再开始下载。"""
    src = _script_text()
    # 代理体检必须在 download 调用之前出现
    i_probe = src.find("代理体检")
    i_dl = src.find("crypto_ls_research.data.download")
    assert i_probe != -1, "脚本里没有代理体检步骤"
    assert i_dl != -1, "脚本里没有 download 调用"
    assert i_probe < i_dl, (
        "代理体检排在下载之后 —— 那就会先撞 stack trace 再报代理问题")


def test_clients_all_read_the_same_proxy_variable():
    """🔴 三个客户端读**同一个**变量名。

    名字不一致的话，改一个端口要改三处，而漏掉的那处只会在换源（binance）
    或实盘下单时才炸 —— 那时已经在下载/交易途中了。
    """
    import pathlib
    repo = pathlib.Path(ROOT)
    files = {
        "okx_client": repo / "crypto_ls_research/data/okx_client.py",
        "binance_client": repo / "crypto_ls_research/data/binance_client.py",
        "okx_private": repo / "crypto_ls_research/execution/okx_private.py",
    }
    for name, p in files.items():
        src = p.read_text(encoding="utf-8")
        assert 'os.environ.get("OKX_PROXY"' in src, (
            f"{name} 没读 `OKX_PROXY` —— 换机器时它会用错的代理端口")


def test_empty_proxy_means_direct_connection():
    """置空 `OKX_PROXY` ⇒ 直连（服务器上常需要），不能被当成"用空代理"。"""
    import subprocess
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from crypto_ls_research.data.okx_client import DEFAULT_PROXY;"
        "print(repr(DEFAULT_PROXY))" % ROOT
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("OKX_")}
    env["OKX_PROXY"] = ""          # 显式空
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-300:]
    assert r.stdout.strip() == "''", (
        f"OKX_PROXY='' 应得到空代理（→ 直连），实得 {r.stdout.strip()}")


# ---------------------------------------------------------------------------
# 重建基线产物（页头读的那份）
# ---------------------------------------------------------------------------
def test_rebuild_script_derives_its_config_from_spec():
    """🔴 `rebuild_optimal_baseline.py` 的参数必须**从 `webapp/spec.py` 派生**。

    `artifacts/` 被 gitignore ⇒ 换机器后页头必然显示「基线数据缺失」。
    要重建就得用**和验收时完全一样**的配置（`OPTIMAL_CLI` + `OPTIMAL_OVERRIDES`）。

    ⚠️ 如果这个脚本自己写一份字面量（`--bar 1h --asset-class all ...`），
    改口径时就会漏改这里 —— 而它产出的正是页头宣称的那几个数，
    错了会让页面报一个**从未被验收过**的 Sharpe。本仓库反复踩过这个坑
    （`rebalance_days` 曾在五处各写一遍）。
    """
    import pathlib
    p = pathlib.Path(ROOT) / "scripts" / "rebuild_optimal_baseline.py"
    assert p.exists(), "缺少 rebuild_optimal_baseline.py"
    src = p.read_text(encoding="utf-8")
    assert "from spec import" in src, "没有从 webapp.spec 导入配置"
    for name in ("OPTIMAL_CLI", "OPTIMAL_OVERRIDES", "OPTIMAL_TAG"):
        assert name in src, f"没有用到 {name} —— 配置可能被写死了"
    # 不许出现写死的因子名（那说明抄了一份而不是派生）。
    # ⚠️ **只看非注释行** —— 脚本的注释里会举 `factors.subset=["momentum",...]`
    # 作为"该长什么样"的例子，扫全文本会命中那段说明（第一版就误报了）。
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    for lit in ("momentum", "rev_short", "range_pos"):
        assert lit not in code, (
            f"脚本的**代码**里出现了写死的因子名 {lit!r} —— 应当从 ACCEPTED_FACTORS 派生")


def test_rebuild_script_serialises_overrides_as_json():
    """⚠️ `--override` 的值走 `json.loads` ⇒ 列表必须写成 **JSON 数组**。

    写 `factors.subset=momentum,flow,...` 会被当成字符串、逐字符解析成
    36 个"因子"，报 `unknown factor(s): ['m','o','m',...]`（实测踩过）。
    所以脚本里必须有 `json.dumps`。
    """
    import pathlib
    src = (pathlib.Path(ROOT) / "scripts" / "rebuild_optimal_baseline.py"
           ).read_text(encoding="utf-8")
    assert "json.dumps" in src, (
        "--override 的值没有走 json.dumps ⇒ 列表会被逐字符解析")


def test_rebuild_defaults_to_base_only():
    """默认**只跑 base** —— 页头只需要它（约 10 秒），全套要很久。

    断言默认值是 `["base"]`：跑全套是 30+ 分钟且会覆盖 charts 等产物。
    """
    import re as _re
    import pathlib
    src = (pathlib.Path(ROOT) / "scripts" / "rebuild_optimal_baseline.py"
           ).read_text(encoding="utf-8")
    m = _re.search(r'add_argument\("--stages".*?default=(\[[^\]]*\])', src, _re.S)
    assert m, "没找到 --stages 的默认值"
    assert m.group(1).replace(" ", "") == '["base"]', (
        f"--stages 默认值是 {m.group(1)}，应当是 [\"base\"]")


# ---------------------------------------------------------------------------
# shell 语法陷阱（真的炸过一次）
# ---------------------------------------------------------------------------
def test_no_bare_variable_before_non_ascii():
    """🔴 `$VAR（` 会被 bash 当成变量名 `VAR（`（含全角括号）⇒ **unbound variable**。

    实测：`echo "end=$END（明天 UTC）"` 在 `set -u` 下直接
    `line 165: END（: unbound variable` 退出。

    ⚠️ **`bash -n` 检查不出来** —— 它只做语法分析，这个错要真的执行到那一行
    才炸。所以必须在这里静态扫。修法是写 `${VAR}`，用花括号界定边界。

    这条属于本项目已经记过的坑（`$VAR（` 吞全角括号），这里是它在
    部署脚本里的复现。
    """
    src = _script_text()
    bad = []
    for i, ln in enumerate(src.split("\n"), 1):
        if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])", ln):
            bad.append(f"    第 {i} 行: {ln.strip()[:90]}")
    assert not bad, (
        "这些行里 `$VAR` 紧接着非 ASCII 字符，bash 会把它并进变量名：\n"
        + "\n".join(bad) + "\n  ⇒ 改成 `${VAR}`")


def test_script_fails_loudly_on_a_fresh_tree():
    """🔴 脚本在**没有产物**的树上必须能跑到最后，不能中途 unbound 退出。

    这条只做静态检查（跑真的会花 1 小时）：确认关键变量都有默认值、
    且 `set -u` 下不会因为某个分支没赋值而炸。
    """
    src = _script_text()
    # 在 `set -euo pipefail` 下，任何可能未赋值的展开都是地雷
    for v in ("END", "BARS", "PROBE", "HAS_HEAD"):
        # 要么有默认值赋值，要么出现在 ${V-...} / ${V:-...} 形式里
        has_assign = re.search(rf'^\s*{v}=', src, re.M)
        has_default = re.search(rf'\$\{{{v}[-:]', src)
        assert has_assign or has_default, (
            f"`{v}` 在 `set -u` 下可能未赋值就被展开 ⇒ 中途 unbound 退出")


def test_rebuild_refuses_to_overwrite_an_existing_baseline():
    """🔴 `rebuild_optimal_baseline.py` **默认不许覆盖已有基线**。

    2026-10-16 我自己踩的：以为在清探测产物，实际那次「实跑」用的是真的
    `OPTIMAL_TAG` ⇒ 覆盖了 `baseline.pkl`（33MB）+ `tables/` 里 10 个文件。
    后果是 `test_failure_attribution.py` 的 5 个回撤窗口立刻失效
    （实测 W2 从 −0.1082 变 −0.1131），而且**换不回来** ——
    `artifacts/**` 被 gitignore、pkl 又没备份。

    所以这个脚本必须：
      * 有 `--force` 开关；
      * 没给 `--force` 且产物已存在时**退出**（不是警告后继续）。
    """
    import pathlib
    import subprocess
    src = (pathlib.Path(ROOT) / "scripts" / "rebuild_optimal_baseline.py"
           ).read_text(encoding="utf-8")
    assert "--force" in src, "没有 --force 开关"
    assert "拒绝覆盖" in src or "refus" in src.lower(), "没有拒绝覆盖的分支"

    # 真跑一次：产物存在时**必须**非零退出
    head = (pathlib.Path(ROOT) / "artifacts" / "v5_1d_all5" / "tables"
            / "01_headline_metrics.json")
    if not head.exists():
        pytest.skip("本机没有基线产物，跳过行为断言")
    r = subprocess.run([sys.executable,
                        str(pathlib.Path(ROOT) / "scripts" / "rebuild_optimal_baseline.py")],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode != 0, (
        "产物已存在时脚本竟然正常退出了 —— 它会静默覆盖已验收基线")
    assert "拒绝覆盖" in (r.stdout + r.stderr), (
        f"没有说清为什么拒绝：(r.stdout+r.stderr)[-300:]")


# ---------------------------------------------------------------------------
# 回测日志 + 失败退出码（2026-10-16：用户问「运行回测失败去哪看日志」）
# ---------------------------------------------------------------------------
def test_research_writes_a_log_file():
    """🔴 `run.research` 必须**自己**把输出写进日志文件。

    原先它 76 个 `print()` 全去 stdout ⇒ **没有任何日志文件**。
    于是「回测失败了去哪看日志」的答案是"没有地方" —— 而回测跑几十分钟，
    窗口一关、或 ssh 断线，失败原因就永远找不回来了。
    （`logs/` 里那几份 9-29 的 `research_*.log` 是我**手工 `> logs/...`**
      重定向出来的，不是代码功能 —— 所以别人重跑时并没有日志。）
    """
    import pathlib
    import subprocess
    src = (pathlib.Path(ROOT) / "crypto_ls_research" / "run" / "research.py"
           ).read_text(encoding="utf-8")
    assert '"--log"' in src, "没有 --log 开关"
    assert 'default="auto"' in src, "日志不是**默认开启**的 —— 用户不会记得加参数"
    assert "_tee_to" in src, "没有把输出分到文件（tee）的实现"

    # 真跑一次（--quick + 只跑 base，几秒），确认文件真的产生
    import glob
    before = set(glob.glob(os.path.join(ROOT, "logs", "research-*-*.log")))
    # ⚠️ **`--max-insts 12` 不是凑数**：这条测试要验证的是"日志会落盘"，
    #    而日志机制与宇宙大小无关。全量（154 只）跑一次要 **105 秒**、
    #    峰值 ~1.8 GB；12 只只要 **9 秒**。
    #    实测：不缩的时候，「整个测试文件一起跑」会把 pytest 进程 OOM 杀掉
    #    （退出码 137，且**没有任何失败信息**，只知道套件突然没了）。
    r = subprocess.run(
        [sys.executable, "-m", "crypto_ls_research.run.research",
         "--stages", "base", "--tag", "__logtest", "--max-insts", "12"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"回测没跑通：{r.stdout[-300:]}\n{r.stderr[-300:]}"
    after = set(glob.glob(os.path.join(ROOT, "logs", "research-*-*.log")))
    new = after - before
    assert new, "跑了回测但 logs/ 里没有新的 research 日志"
    body = open(sorted(new)[-1], encoding="utf-8").read()
    assert "research run:" in body, f"日志里没有运行信息：{body[:200]}"
    # 清理
    for p in new:
        os.remove(p)
    import shutil
    shutil.rmtree(os.path.join(ROOT, "artifacts", "__logtest"), ignore_errors=True)


def test_a_failed_stage_makes_the_process_exit_nonzero():
    """🔴 **阶段失败必须非零退出。** 原先它只打印 traceback 然后**退出码 0**。

    后果很具体：`init_linux.sh` / CI / 任何 `.sh` 里 `cmd && next` 的写法
    都会把一次**失败**的回测当成成功，继续往下走；
    而产物只写了一半，下一个读的人无从判断它是完整还是残缺。
    （实测过：故意传一个不存在的因子，老代码退出码是 0。）
    """
    import subprocess
    # ⚠️ 用**不存在的 stage 名**来制造失败，**不要**用"不存在的因子名"。
    #
    #   因子名校验发生在 `run_backtest` 里 —— 走到那里说明**面板已经全加载
    #   完了**（154 只 × 50k bars，峰值 ~1.8 GB）。而这条测试只是想验证
    #   "失败会非零退出"，不需要为此付一次完整回测的内存。
    #   实测：旧写法**单跑就把测试进程 OOM 杀掉**（退出码 137）。
    #
    #   拼错的 stage 名在**调度循环里**就被发现（`fn is None`），
    #   0 秒返回、一个面板都不加载。
    r = subprocess.run(
        [sys.executable, "-m", "crypto_ls_research.run.research",
         "--stages", "nosuchstage", "--tag", "__failtest"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode != 0, (
        "回测失败了但退出码是 0 —— 调用者会以为成功")
    combined = r.stdout + r.stderr
    assert "FAILED stages" in combined, (
        f"没有明确说哪些阶段失败：{combined[-300:]}")
    assert "不完整" in combined, "没提醒产物是不完整的"
    import glob
    for p in glob.glob(os.path.join(ROOT, "logs", "research-__failtest-*.log")):
        os.remove(p)
    import shutil
    shutil.rmtree(os.path.join(ROOT, "artifacts", "__failtest"), ignore_errors=True)


# ---------------------------------------------------------------------------
# 小内存机器（2GB）能不能跑：`load_panels` 的内存纪律
# ---------------------------------------------------------------------------
def test_read_one_downcasts_floats_to_float32():
    """🔴 `_read_one` 读进来就把 float64 降到 float32。

    实测（154 只 × 50.7k bars 的 1h 宇宙）：`load_panels` 一步吃掉 1.14 GB，
    而最终 8 个宽表只需 ~320 MB —— 差额全在临时对象上，其中最大一块是
    `raw` 里的 float64 单只 DataFrame（3.25 MB → 1.83 MB，**省 44%**）。

    用户那台 2GB 的 Ubuntu 跑全量宇宙时被 OOM 杀（退出码 **-9**），
    日志停在 `panels: ... instruments` 之后 —— 正是这一步。

    ⚠️ **这不改变数值**：`wide()` 最后本来就 `astype("float32")`。
    这里只是把那次转换**提前**，让 float64 的峰值根本不出现。
    已验证：开关两侧的 `bars` 末行和逐位相同（`31.508383622809`）。
    """
    import pathlib
    src = (pathlib.Path(ROOT) / "crypto_ls_research" / "data" / "store.py"
           ).read_text(encoding="utf-8")
    assert "def _read_one(path: str, downcast: bool = True)" in src, (
        "`_read_one` 没有 downcast 开关")
    assert '"float32"' in src, "没有降到 float32"


def test_load_panels_releases_raw_after_building_wide_tables():
    """🔴 宽表建完必须 `del raw`。

    `raw` 是逐只 DataFrame（实测 ~290 MB），而后面只用到 `close` 与 `fund`
    —— 留着它等于白占一份内存，在 2GB 机器上就是 OOM 的边缘。
    """
    import pathlib
    src = (pathlib.Path(ROOT) / "crypto_ls_research" / "data" / "store.py"
           ).read_text(encoding="utf-8")
    i_panels = src.find("panels = {f: wide(f) for f in FIELDS}")
    assert i_panels != -1, "没找到宽表构造"
    i_del = src.find("del raw", i_panels)
    assert i_del != -1, (
        "宽表构造之后没有 `del raw` —— 逐只 DataFrame 会一直占着内存")


def test_downcast_does_not_change_results():
    """降 float32 **不许**改变回测结果（这是它能开着的唯一理由）。

    跑两次：一次默认（float32），一次强制 float64，比 `bars` 的数值和。
    用 `_read_one` 的 `downcast` 参数切换，不动全局状态。

    ⚠️ 这条要真跑回测（几秒）。数组末行和逐位相同才算过 ——
    用 `==` 而不是 `np.allclose`：只要浮点路径变了它就该红。
    """
    import subprocess
    code = '''
import sys
sys.path.insert(0, %r)
import crypto_ls_research.data.store as S
from crypto_ls_research.data.asset_class import load_categories, filter_insts
from crypto_ls_research.data.store import list_cached_insts
from crypto_ls_research.run import research as R
from crypto_ls_research.backtest.engine import run_backtest

cats = load_categories()
# ⚠️ **只取 40 只，不用全宇宙**。降不降 float32 会不会改变结果，
#    40 只 × 50k bars 完全够证明；用 154 只会让这个子进程的峰值
#    再涨 ~1 GB，而 pytest 主进程的常驻量并不因此变小 ——
#    实测「测试文件整体跑」时就是这样被 OOM 杀掉（退出码 137）的。
scoped = sorted(filter_insts(list_cached_insts("1h"), cats, "crypto")[0])[:40]
if not scoped:
    print("SKIP")
    sys.exit(0)
R.BASE_OVERRIDES.update({
    "factors.subset": ["momentum", "flow", "range_pos", "hitrate", "rev_short"],
    "portfolio.max_weight_per_instrument": 0.20,
    "execution.max_daily_turnover": 0.20,
})
orig = S._read_one
out = {}
for label, dc in (("f32", True), ("f64", False)):
    S._read_one = (lambda p, downcast=dc, _o=orig: _o(p, downcast=downcast))
    p = S.load_panels("1h", "2021-01-01", "2026-09-26", insts=scoped)
    res = run_backtest(p, R.base_cfg("1h", 24, "2021-01-01", "2026-09-26"))
    b = res.bars
    out[label] = [float(b[c].iloc[-1]) for c in b.columns if b[c].dtype.kind == "f"]
    # 每次用完就丢：**同一个子进程里要跑两遍**，不丢会叠加
    del p, res, b
    import gc; gc.collect()
print("RESULT " + repr(out))
''' % ROOT
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    if "SKIP" in r.stdout:
        pytest.skip("本机没有 crypto 宇宙的缓存")
    assert r.returncode == 0, f"跑不动：{r.stderr[-500:]}"
    line = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
    assert line, f"没拿到结果：{r.stdout[-300:]}"
    d = eval(line[0][len("RESULT "):])          # noqa: S307 - 本地自产
    assert d["f32"] == d["f64"], (
        f"float32 改变了回测结果：\n  f32={d['f32']}\n  f64={d['f64']}")


# ---------------------------------------------------------------------------
# 飞书通知：配置 CLI（2026-10-16：用户问「怎么添加飞书通知来着」）
# ---------------------------------------------------------------------------
def test_notify_cli_exists_and_documents_itself():
    """🔴 `notify.py` 必须能当命令跑（`--set-url` / `--show` / `--test`）。

    为什么重要：`config/notify.json` 被 **gitignore**（里面有 webhook URL，
    拿到它谁都能往群里发消息）⇒ **每台新机器都要配一次**。
    如果没有 CLI，用户只能手抄 JSON —— 而模板里还有 `enabled` 忘了改的坑
    （`--set-url` 会自动写 `enabled: true`，手改很容易漏）。

    `--set-url` / `--set-secret` / `--set-keyword` 都会**写完立刻自检**，
    所以"配没配对"当场就知道，而不是等下次调仓才发现推不出去。
    """
    import pathlib
    import subprocess
    src = (pathlib.Path(ROOT) / "crypto_ls_research" / "execution" / "notify.py"
           ).read_text(encoding="utf-8")
    for flag in ("--test", "--show", "--set-url", "--set-secret", "--set-keyword"):
        assert f'"{flag}"' in src, f"notify CLI 少了 {flag}"

    r = subprocess.run(
        [sys.executable, "-m", "crypto_ls_research.execution.notify", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"--help 跑不起来：{r.stderr[-300:]}"
    out = r.stdout + r.stderr
    for flag in ("--test", "--show", "--set-url", "--set-secret", "--set-keyword"):
        assert flag in out, f"--help 里没有 {flag}"


def test_notify_show_redacts_the_webhook_url():
    """🔴 `--show` 必须把 URL 打码。

    webhook URL 的**最后一段就是凭据** —— 拿到它就能往群里发任意消息。
    而 `--show` 的输出很容易被贴进 issue / 聊天记录里求助，
    所以必须打码，不能图省事原样打出来。
    """
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "crypto_ls_research.execution.notify", "--show"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    # 无论本机配没配，都不该出现完整的 hook token
    import re as _re
    m = _re.search(r"/hook/([A-Za-z0-9_-]{8,})", out)
    assert not m, f"--show 泄露了完整 webhook token：{m.group(0)[:60]}"


# ---------------------------------------------------------------------------
# 控制台被 OOM 杀时的提示（2026-10-16：用户点「一键准备」撞的）
# ---------------------------------------------------------------------------
def test_start_console_explains_a_sigkill_exit():
    """🔴 `START_CONSOLE.command` 遇到 `137`/`9` 必须**翻译成人话**。

    `137 = 128 + 9`（`9` = SIGKILL）⇒ 被 OOM killer 杀了，**不是崩溃**。
    原先脚本只打 `[exit code 137]`，用户看到的是
    「Killed ... [exit code 137] ... read: read error: 0: Bad file descriptor」
    —— 完全看不出是内存问题（用户当时的原话就是「啥意思」）。

    ⚠️ **具体的触发点**：控制台的「⚡ 一键准备（刷新行情＋生成计划）」
    会跑一次完整回测（`target()` 里写着「正在计算信号（复用回测引擎）」），
    峰值 ~2.0 GB；加上控制台自身 182 MB ≈ **2.1 GB**。
    """
    import pathlib
    src = (pathlib.Path(ROOT) / "START_CONSOLE.command").read_text(encoding="utf-8")
    assert '"137"' in src and '"9"' in src, "没有识别 SIGKILL 退出码"
    assert "OOM" in src or "内存" in src, "没有说明真实原因（内存不够）"
    assert "swap" in src, "没有给出修法（加 swap）"
    # ⚠️ 不能把正常退出也喷一堆字
    assert '"$rc" != "0"' in src or '!= "0"' in src, (
        "没有把「正常退出」与「异常退出」分开 —— 会污染正常启动的输出")


def test_deploy_doc_covers_the_console_oom_path():
    """文档必须提到控制台那条路，不能只讲命令行回测。

    用户是在**控制台**里撞的，而原先文档只写了
    「回测跑到 panels: ... 戛然而止」那个命令行症状 —— 对不上他的现象。
    """
    import pathlib
    doc = (pathlib.Path(ROOT) / "DEPLOY_LINUX.md").read_text(encoding="utf-8")
    assert "一键准备" in doc, "没提控制台的「一键准备」这条触发路径"
    assert "137" in doc, "没提 137 这个退出码"
    assert "仅刷新行情" in doc, "没说明「仅刷新行情」是安全的（不含回测）"


# ---------------------------------------------------------------------------
# 内存：`wide()` 的拷贝次数（2026-10-16：用户 2GB 机器普通回测也 OOM）
# ---------------------------------------------------------------------------
def test_wide_builds_in_one_pass():
    """🔴 `load_panels().wide()` 必须**一步**建成 float32 宽表。

    原写法是三步，每步都分配一份**全量**矩阵：

        pd.DataFrame(cols)      # float64，62 MB
          .reindex(grid)        # 又一份 float64，62 MB
          .astype("float32")    # 再一份 float32，31 MB

    50k × 154 下，8 个字段 × 3 份 ⇒ 瞬时峰值 ~1.5 GB。
    实测：`load_panels` 之后常驻内存 **973 MB → 555 MB（省 418 MB）**。

    ⚠️ **数值必须逐位不变** —— 已用 40 只 × 50k bars 对拍过
    （NaN 位置相同、逐位相同、最大差 0.0）。
    """
    import pathlib
    src = (pathlib.Path(ROOT) / "crypto_ls_research" / "data" / "store.py"
           ).read_text(encoding="utf-8")
    i = src.find("def wide(field: str)")
    assert i != -1, "找不到 wide()"
    body = src[i:i + 1200]
    # 一步构造：DataFrame(cols, index=grid, dtype="float32")
    assert "index=grid" in body and 'dtype="float32"' in body, (
        "wide() 没有一步构造 float32 宽表（仍会做多次全量拷贝）")
    assert ".reindex(grid)" not in body, (
        "wide() 里还在 reindex —— 那会多分配一份全量矩阵")
    assert ".astype(\"float32\")" not in body, (
        "wide() 里还在单独 astype —— 应由构造器的 dtype= 完成")


def test_wide_one_pass_is_bit_identical():
    """一步构造与三步构造**逐位相同**（这是它敢开着的唯一理由）。"""
    import subprocess
    code = '''
import sys, numpy as np, pandas as pd, os
sys.path.insert(0, %r)
from crypto_ls_research.data.asset_class import load_categories, filter_insts
from crypto_ls_research.data.store import list_cached_insts, _read_one, SECONDS_PER_BAR
from crypto_ls_research.config.settings import CACHE_DIR
cats = load_categories()
scoped = sorted(filter_insts(list_cached_insts("1h"), cats, "crypto")[0])[:40]
if not scoped:
    print("SKIP"); sys.exit(0)
cdir = os.path.join(CACHE_DIR, "candles", "1h")
raw = {}
for i in scoped:
    d = _read_one(os.path.join(cdir, i + ".parquet"))
    if d is not None:
        raw[i] = d
grid = pd.date_range("2021-01-01", "2026-09-26",
                     freq=pd.Timedelta(seconds=SECONDS_PER_BAR["1h"]),
                     inclusive="left", tz="UTC")
cols = {i: d["close"] for i, d in raw.items()}
old = pd.DataFrame(cols).reindex(grid).astype("float32")
new = pd.DataFrame(cols, index=grid, dtype="float32")
a = old.fillna(-1).to_numpy(); b = new.fillna(-1).to_numpy()
print("RESULT", bool((old.isna() == new.isna()).all().all()), bool((a == b).all()))
''' % ROOT
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    if "SKIP" in r.stdout:
        pytest.skip("本机没有 crypto 宇宙的缓存")
    assert r.returncode == 0, f"跑不动：{r.stderr[-400:]}"
    line = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
    assert line, f"没拿到结果：{r.stdout[-200:]}"
    # ⚠️ 打印的是 `RESULT True True`（空格分隔），**不是元组字面量** ——
    #    第一版写 `eval(...)` 直接 SyntaxError（子进程里报错、测试却只说"跑不动"）。
    nan_ok, val_ok = (x == "True" for x in
                      line[0][len("RESULT "):].split())
    assert nan_ok and val_ok, (
        f"一步构造与三步构造结果不同：NaN 位置相同={nan_ok} 逐位相同={val_ok}")
