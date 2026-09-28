# 自动交易 · 启动说明（Windows / macOS 双平台）

## 一、你现在要的东西

**每天在信号产生后立即自动下单（模拟盘 demo），跟随 v3 回测口径。**

| 平台 | 启动 | 停止 | 每日定时 |
|---|---|---|---|
| **macOS** | 双击 `START_AUTO_TRADER_DEMO.command` | 双击 `STOP_AUTO_TRADER.command` | `python3 scripts/install_scheduler.py --install` |
| **Windows** | 双击 `START_AUTO_TRADER_DEMO.bat` | 双击 `STOP_AUTO_TRADER.bat` | 见下方「Windows 定时」 |

> macOS 首次双击若报「来自身份不明的开发者」：**右键 → 打开 → 仍要打开**。
> 或先执行一次 `chmod +x *.command`。

## 一·补、⚠️ 参数中台里的「启动自动任务」按钮为什么点不通

**不是 bug，也修不了 —— 是中台自己启动的方式决定。** 详见本节。

参数中台的 `#live` 页有一个「启动自动任务」按钮，它会 spawn 一个常驻守护进程。
如果 `webapp/server.py` 是从一个**短命 shell**（IDE 任务、工具调用、sandbox 包装）
里起的，那么它 spawn 的守护进程属于**同一条进程树**，会在启动期
（增量刷新那 ~38 秒）被**静默回收**：

- 端点返回 `ok: true`（进程确实起来了）
- `auto_stdout.log` **断在半句**，没有 traceback
- `is_running()` 几秒后变回 `False`

实测证据（2026-09-29）：点按钮后 pid 4696 启动，日志最后两行是
`行情偏旧，先做增量刷新…` / `增量刷新 1h K 线缓存…` —— 然后就没了。
父进程链：`WorkBuddy.exe → sandbox-cli.exe → bash.exe → python webapp/server.py 8790`
—— **中台本身就在沙箱进程树里**。

### 解法：用 `START_CONSOLE.bat` / `START_CONSOLE.command` 启动中台

| 平台 | 中台启动方式 |
|---|---|
| **Windows** | 双击 **`START_CONSOLE.bat`** |
| **macOS / Linux** | 双击 **`START_CONSOLE.command`** |

双击启动的中台，父进程是 `explorer.exe` / Finder，**不在任何会被回收的进程树里**，
于是它派生的守护进程能活下来 —— 按钮才真正可用。

> 反过来：**如果你不打算用网页按钮，只想让自动交易跑起来，
> 直接双击 `START_AUTO_TRADER_DEMO.bat` 更省事**，根本不需要中台。

## 二、⚠️ 必须先读：下单时刻到底在几点

**下单时刻由回测口径决定，不由任何定时器决定。** 三段链条如下：

| 环节 | 时刻（北京） | 来源 |
|---|---|---|
| ① 调仓信号产生 | **10:00** | 网格锚点写死在数据里（UTC 02:00，689 个调仓点全部如此） |
| ② 回测执行价取价 | **11:00** | `exec_price = next_open`（下一根 bar 的开盘价） |
| ③ 守护进程自然到期 | **11:00** | `due = bars_since_decision >= R`，面板确认那根 bar 后打开 |

**三者天然对齐** —— 所以「信号产生后立即下单」的正确做法就是
**让守护进程按自己的判据跑，不要加执行窗口**。当前默认即为此：

```
执行窗口    : 无（严格跟随回测网格）
```

> 为什么定时器默认设在 **11:05** 而不是 10:00：10:00 时那根 bar 刚收盘、
> 数据面板还没确认它，`bars_since = 0`、`due = False`；那一刻拉起来也只能
> 空转到 11:00。11:05 是「执行价所在 bar 确认之后」的第一个 5 分钟点。

### 想把下单挪到别的时刻（例如 23:30）

那就要用执行窗口，**并接受它偏离回测网格**：

```bash
# macOS / Linux：把下单强推到 23:30
EXEC_WINDOW=23:30 ./START_AUTO_TRADER_DEMO.command
```
```bat
:: Windows
set EXEC_WINDOW=23:30
START_AUTO_TRADER_DEMO.bat
```

| 方案 | 做法 | 后果 |
|---|---|---|
| **A. 不加窗口**（当前默认） | 守护进程按 `due` 自然到期 | 下单 = 信号产生后立即。**与回测口径一致。** |
| **B. 加执行窗口** | 到点**强制**调仓一次 | 信号日不变，下单推迟数小时。**绩效不再是 v3 口径。** |
| C. 移回测锚点 | 改 `dec_offset_bars` | 内部一致，但全部既有结论要重跑（已决定不做） |

**实测数据**（`artifacts/grid_phase/REPORT.md`）：把锚点平移到 23:00，
Sharpe 从 1.9301 → 1.7430（−0.187）；但**全 25 个相位的 std 就有 0.1108**，
且 `k=24` 与 `k=0` 同为北京 10:00 却差 0.246 —— 所以这些差距**大半是噪声**。

> ⚠️ **绝不要把窗口设成 10:00。** 那时最新决策点就是刚产生的 bar，窗口会
> 每天照发一遍同样的目标书，把换手预算烧在重复交易上，而日志里看不出异常。

## 二·补、实时风控限额（不改会被拦到一单不成交）

`LiveLimits` 的默认值是为**首次小额实盘**设的，对 $54k 的模拟盘太紧：

| 限额 | 默认 | 现在 | 说明 |
|---|---|---|---|
| `max_gross_notional` | $5,000 | **$30,000** | 默认 < 目标毛敞口 $19,400 → **拦死** |
| `max_order_notional` | $1,000 | **$2,500** | 默认 < 均单 $1,078 → 部分腿被逐笔拦 |

文件：`artifacts/live/demo/limits.json`。改法：

```python
from crypto_ls_research.execution.store import Store
from crypto_ls_research.execution.limits import LiveLimits
st = Store("demo")
lim = LiveLimits.from_dict(st.load_limits())
lim.max_gross_notional = 30_000.0
lim.max_order_notional = 2_500.0
st.save_limits(lim.to_dict())
```

> ⚠️ 这是**绝对风控闸门**，不是策略参数。它是防止一个 bug 一次性打光账户的
> 最后一道墙。调高它意味着接受更大的一次性损失上限。**上实盘前重新评估。**


## 三、当前配置

| 项 | 值 |
|---|---|
| 模式 | `demo`（OKX 模拟盘，真实撮合，**不花真钱**） |
| 调仓网格 | 每 **1 天**（`rebalance_days=1`） |
| 检查间隔 | 每 **30 分钟** |
| 执行窗口 | **无**（严格跟随回测网格，下单＝信号产生后立即） |
| 下单时刻 | 北京 **11:00** 前后（网格 10:00 产生信号 → 下一根 bar 确认后执行） |
| bar | `1h` |
| 资产分类 | `crypto`（134 个加密合约） |
| 通知 | 飞书机器人，推 `traded` / `blocked` / `error` |

配置来源：`engine.DEFAULT_SIGNAL`（v3 已验收配置）+ 启动器覆写的字段。

## 四、macOS 定时（launchd）

```bash
# 默认：11:05 拉起守护进程（确保那一刻在跑），不偏离回测口径
python3 scripts/install_scheduler.py --install

# 改时间
python3 scripts/install_scheduler.py --install --at 11:05

# 执行窗口模式：到点强制调仓一次（偏离回测网格）
python3 scripts/install_scheduler.py --install --mode window --at 23:30

# 查看 / 卸载
python3 scripts/install_scheduler.py --status
python3 scripts/install_scheduler.py --uninstall
```

**为什么用 launchd 而不是 cron**：
1. cron 在笔记本休眠时**直接跳过**，醒来也不补；launchd 唤醒后会补跑。
2. macOS 自 Catalina 起 cron 需要额外「完全磁盘访问」授权，launchd 不需要。

**定时器干什么**：只在守护进程**不在**时拉起它（幂等）。
每天到点不会产生第二个进程——两个循环会各自读同一本书、各下全量单。
详见 `scripts/scheduled_tick.py` 的文件头。

Linux 同样支持（自动改用 systemd user timer，`Persistent=true`）。

## 五、Windows 定时

launchd 是 macOS 的。Windows 用任务计划程序：

```bat
schtasks /Create /TN "CryptoLS-AutoTrader-Demo" /SC DAILY /ST 11:05 ^
  /TR "\"C:\Users\50651\.workbuddy\binaries\python\envs\default\Scripts\python.exe\" \"%CD%\scripts\scheduled_tick.py\" --mode timer" ^
  /F
```

查看 / 删除：

```bat
schtasks /Query /TN "CryptoLS-AutoTrader-Demo" /V /FO LIST
schtasks /Delete /TN "CryptoLS-AutoTrader-Demo" /F
```

> Windows 的 `schtasks` **没有**「错过后补跑」的默认行为。要补跑需在
> 任务计划的图形界面里勾选「如果错过计划开始时间，请尽快运行任务」。
>
> 更稳的做法：**让守护进程常驻**（双击 `.bat` 后不关窗口），
> 定时器只做「死后拉起」的兜底。

## 六、前置条件（启动器会自检，不满足拒绝启动）

1. **通知已配置** —— 无人值守的盘必须能报警
   ```
   python -m crypto_ls_research.execution.notify --show
   ```
2. **demo 密钥有效** —— `config/okx_creds.json` 的 `demo` 段
3. **净值 > 0** —— 启动器会先读一次账户

退出码：`2` 通知未配 / `3` 账户读不到 / `4` 已在跑 / `5` 启动失败 / `6` 无心跳。

## 七、命令行等价写法

```bash
# 启动（默认口径：无窗口，下单＝信号产生后立即）
python -m crypto_ls_research.execution.auto_ctl --mode demo \
       --rebalance-days 1 --interval 30

# 想强制挪到别的时刻（偏离回测网格）
python -m crypto_ls_research.execution.auto_ctl --mode demo \
       --rebalance-days 1 --interval 30 --exec-window 23:30

# 看状态（含窗口与强制次数）
python -m crypto_ls_research.execution.auto_ctl --status --mode demo

# 停止
python -m crypto_ls_research.execution.auto_ctl --stop --mode demo
```

## 八、执行窗口的护栏

> 当前**默认不开**。以下护栏只在显式传入 `--exec-window` 时生效。

1. **默认关闭**。不传 `--exec-window` 时行为与历史完全一致（有测试守着）。
2. **限频**：每 `rebalance_days` 最多强制 **1 次**。否则窗口宽度填错 +
   30 分钟 tick 会一夜烧光 20%/日的换手预算。
3. **留痕**：每次强制写入 `forced_by: "exec_window"` 与
   `signal_age_hours`，并打进通知、审计日志和心跳。
4. **时区显式**：窗口标签是 `23:30 UTC+08:00 (+5min)` 而不是裸 `23:30`
   —— 中文 Windows 的 `%Z` 返回「中国标准时间」，会在 GBK 控制台崩。
5. ⚠️ **不要设成 10:00**（信号刚产生的那一刻）：最新决策点就是那根 bar，
   窗口会每天照发一遍同样的目标书，把换手预算烧在重复交易上。

## 九、文件位置

| 文件 | 内容 |
|---|---|
| `START_CONSOLE.bat` / `.command` | 启动参数中台（**网页按钮要能用就必须用它启动**） |
| `artifacts/live/demo/auto.json` | 心跳：pid / 网格 / 窗口 / 计数 / 上次结论 |
| `artifacts/live/demo/auto.jsonl` | 决策审计（追加写，含强制调仓记录） |
| `artifacts/live/demo/auto_stdout.log` | 守护进程标准输出（**断在半句 = 被进程树回收**） |
| `artifacts/grid_phase/phase_sweep.csv` | 25 个锚点相位的实测绩效 |
| `logs/scheduler.out.log` | 定时器输出（macOS/Linux） |

## 十、两个必须知道的限制

1. **`auto.pid` 与 `auto.json` 的 pid 曾经不一致**（Windows 上 `python -m`
   先起启动器再 re-exec）。已修：心跳出现后控制文件收敛到守护进程真实 pid，
   原启动器 pid 存在 `spawned_pid`。判断「是否在跑」永远看 `is_running()`。

2. **守护进程无法由 AI 代为常驻**：任何从工具调用里 spawn 的进程会在该调用
   返回时被整个进程树回收，`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
   CREATE_BREAKAWAY_FROM_JOB` 全无效。**必须双击脚本，或由定时器/常驻进程派生。**
