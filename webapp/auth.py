"""账号 / 口令 / TOTP 二重认证 / 会话 —— **纯标准库，无第三方依赖**。

为什么要有这个模块
------------------
控制台原本**没有任何鉴权**：它绑在 127.0.0.1 上，于是"只有本机能访问"被当成了
安全边界。但那是**本机进程之间**没有边界 —— 同一台机器上任何一个进程（浏览器里
的任意网页、你随手跑的一个脚本、一个装错包的 CLI）都能直接
`POST /api/live/execute` 下**真钱单**，或者 `GET /api/live/creds` 读走 API key。
一旦要把监听放到外网，这道墙就彻底不存在了。

这个模块提供三件事，各自解决一个**具体的**失败：

1. **口令**（`hash_password` / `verify_password`）
   用 `hashlib.scrypt`，每个账号独立 salt。存的是 `scrypt$n$r$p$salt$hash`，
   自描述参数 ⇒ 将来调参数不需要迁移旧记录（旧串自己带着旧参数）。
   ⚠️ 用 `hmac.compare_digest` 比较，不用 `==`：`==` 会在第一个不同的字节返回，
   把"口令对不对"变成一条**可以按字节猜的侧信道**。

2. **TOTP**（RFC 6238，`totp_now` / `verify_totp`）
   30 秒一步、6 位、HMAC-SHA1 —— 与 Google Authenticator / 1Password / Authy
   一致。手写而不是拉 `pyotp`：这个项目所有依赖都被审计过，为一个 20 行的
   算法引入一个包不值得。
   ⚠️ **接受 ±1 步**（时钟漂移是常态），但**拒绝重放**：同一个码用过就记下来，
   窗口内不再接受第二次。否则一个被肩窥到的码在 30 秒内可以无限次使用。

3. **会话**（`new_session` / `resolve_session`）
   `secrets.token_urlsafe` 生成，**只存哈希**（落盘被读取也拿不到活 token）。
   带过期时间；每一次成功请求**不滚动续期**（固定绝对过期，见 `SESSION_TTL`）。

落盘
----
`config/auth.json`，权限 0600，原子替换（临时文件 + `os.replace`），
**口令哈希与 TOTP 密钥都不进日志**。文件里没有任何明文可用的凭据。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

#: 仓库根。⚠️ `webapp/` 只在根下**一层**，所以 `dirname(__file__)` 就是 `webapp/`、
#: 再一层就是根 —— **只需一次 dirname**。
#:
#: 🔴 我第一版是从 `crypto_ls_research/execution/credentials.py` 抄的，而那个文件在
#: 根下**两层**，模式是 `dirname(dirname(__file__))` + `..`。照搬到一层深的文件上
#: 就多爬了一级 ⇒ `AUTH_FILE` 落到 `~/code/config/auth.json`（仓库**外面**），
#: 后果有三：
#:   1. `.gitignore` 里的 `config/auth.json` 根本管不到它 —— 真实凭据在仓库外裸奔；
#:   2. 删掉仓库内的 `config/auth.json` 也"删不掉"账号（服务照旧 configured）；
#:   3. 换台机器/换个 clone 时凭据"消失"，表现为登录不上而毫无线索。
#: 守卫 `test_auth.py::test_auth_file_lives_inside_the_repo` 钉住这件事。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
AUTH_FILE = os.path.join(CONFIG_DIR, "auth.json")

#: 会话绝对有效期（秒）。**不做滑动续期** —— 一个"只要你一直在用就永不过期"的
#: 会话，等价于把有效期设成无限，而这台机器上能下单。8 小时够一个工作日。
SESSION_TTL = 8 * 3600

#: TOTP 参数。改这些会让**所有人已绑定的验证器失效**，所以写死在代码里而不是
#: 做成配置项：它不该被"顺手调一下"。
TOTP_DIGITS = 6
TOTP_STEP = 30
TOTP_ALGO = "sha1"          # 验证器 App 的默认，别改
TOTP_WINDOW = 1             # 接受 ±1 步（±30s）时钟漂移

#: 登录失败节流：连续失败到这个次数就锁一段时间。
#: ⚠️ 这两个数是**防在线暴力猜**的，不是防离线爆破（那是 scrypt 的活）。
MAX_FAILS = 5
LOCKOUT_SEC = 300

_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------
#: scrypt 参数。n=2**14 在现代硬件上约 50–100ms —— 足以让在线猜测不划算，
#: 又不至于让每次登录卡顿。⚠️ 调高会让**已有**哈希仍按自己带的参数验证
#: （串是自描述的），所以调参不需要迁移。
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(pw: str) -> str:
    """`scrypt$n$r$p$<b64 salt>$<b64 hash>` —— 自描述，便于将来调参。"""
    if not isinstance(pw, str) or not pw:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt,
                       n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode("ascii")     # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(h)}"


def verify_password(pw: str, stored: str) -> bool:
    """**恒定时比较**。任何解析失败都返回 False（不抛）。"""
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
        got = hashlib.scrypt((pw or "").encode("utf-8"), salt=salt,
                             n=int(n), r=int(r), p=int(p), dklen=len(want))
        return hmac.compare_digest(got, want)
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# TOTP（RFC 6238）
# ---------------------------------------------------------------------------
def new_totp_secret() -> str:
    """base32（无填充）—— 验证器 App 认的标准形式。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _b32_decode(s: str) -> bytes:
    s = (s or "").strip().replace(" ", "").upper()
    pad = (-len(s)) % 8
    return base64.b32decode(s + "=" * pad)


def totp_at(secret: str, when: Optional[float] = None) -> str:
    """给定时刻的 6 位码。`when` 可注入 ⇒ 测试不必等 30 秒。"""
    counter = int((when if when is not None else time.time()) // TOTP_STEP)
    key = _b32_decode(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, getattr(hashlib, TOTP_ALGO)).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def totp_now(secret: str) -> str:
    return totp_at(secret, None)


def verify_totp(secret: str, code: str, *, when: Optional[float] = None,
                window: int = TOTP_WINDOW,
                used: Optional[set] = None) -> Tuple[bool, str]:
    """校验一个 TOTP 码。返回 `(ok, reason)`。

    ⚠️ **重放防护**：`used` 传入一个集合，用过的 (counter) 会记进去；命中
    就拒绝。没有这一条，一个被看到/被截的码在它那 30 秒内可以反复用 ——
    而这正是二重认证唯一要防的窗口。
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return False, "格式不对"
    now = when if when is not None else time.time()
    base = int(now // TOTP_STEP)
    for off in range(-window, window + 1):
        c = base + off
        # 逐位比较仍然用 compare_digest（同口令那条理由）
        if hmac.compare_digest(totp_at(secret, c * TOTP_STEP), code):
            if used is not None:
                if c in used:
                    return False, "该验证码已经用过"
                used.add(c)
            return True, "ok"
    return False, "验证码不对或已过期"


def otpauth_url(secret: str, account: str, issuer: str) -> str:
    """给验证器 App 扫的标准 otpauth:// URL。"""
    from urllib.parse import quote, urlencode
    q = urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": TOTP_ALGO.upper(),
        "digits": TOTP_DIGITS,
        "period": TOTP_STEP,
    })
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?{q}"


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def _token_hash(tok: str) -> str:
    """只存哈希：auth.json 被读走也拿不到能用的 token。"""
    return hashlib.sha256((tok or "").encode("utf-8")).hexdigest()


class AuthStore:
    """账号 + 会话的持久层。**单用户**，但结构上留了多用户的余地。

    文件形状：
    ```json
    {
      "user": {"name": "...", "pw": "scrypt$...", "totp": "BASE32",
               "created": 123.0},
      "sessions": {"<sha256(token)>": {"exp": 123.0, "ua": "..."}},
      "fails": {"n": 3, "until": 0.0, "used_totp": [55555, 55556]}
    }
    ```
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or AUTH_FILE

    # -- 读写 ---------------------------------------------------------------
    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            # 读不到 = 还没建账号。**不是**"因此放行" —— 调用方必须靠
            # `is_configured()` 判断，未配置时走首次设置流程。
            return {}

    def _write(self, d: dict) -> None:
        d = dict(d)
        d["_saved"] = time.time()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)      # 口令哈希也不该给别人看
        except OSError:
            pass

    # -- 账号 ---------------------------------------------------------------
    def is_configured(self) -> bool:
        u = self._read().get("user") or {}
        return bool(u.get("name") and u.get("pw") and u.get("totp"))

    def user(self) -> dict:
        return self._read().get("user") or {}

    def create_user(self, name: str, password: str) -> str:
        """建账号，返回 TOTP 密钥（**只在这一刻返回明文**，之后拿不到）。

        ⚠️ 已存在就抛 —— 静默覆盖会把已有账号的验证器悄悄换掉，而用户下次
        登录时只会看到"验证码不对"。
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("用户名不能为空")
        if len(password or "") < 8:
            raise ValueError("口令至少 8 位")
        with _LOCK:
            d = self._read()
            if (d.get("user") or {}).get("pw"):
                raise ValueError("账号已存在；要重设请先删除 auth.json")
            secret = new_totp_secret()
            d["user"] = {"name": name, "pw": hash_password(password),
                         "totp": secret, "created": time.time()}
            d.setdefault("sessions", {})
            d["fails"] = {"n": 0, "until": 0.0, "used_totp": []}
            self._write(d)
            return secret

    # -- 节流 ---------------------------------------------------------------
    def locked_for(self) -> float:
        """还要锁多少秒（0 = 没锁）。"""
        f = self._read().get("fails") or {}
        left = float(f.get("until") or 0.0) - time.time()
        return max(0.0, left)

    def note_fail(self) -> dict:
        """记一次失败；达到阈值就锁定。返回当前节流状态。"""
        with _LOCK:
            d = self._read()
            f = dict(d.get("fails") or {})
            f["n"] = int(f.get("n") or 0) + 1
            if f["n"] >= MAX_FAILS:
                f["until"] = time.time() + LOCKOUT_SEC
                f["n"] = 0                       # 锁满一轮就归零，避免永久锁死
            d["fails"] = f
            self._write(d)
            return {"n": f["n"], "until": f["until"],
                    "locked_for": max(0.0, f["until"] - time.time())}

    def note_ok(self) -> None:
        with _LOCK:
            d = self._read()
            d["fails"] = {"n": 0, "until": 0.0,
                          "used_totp": ((d.get("fails") or {}).get("used_totp") or [])}
            self._write(d)

    def totp_used(self) -> set:
        return set((self._read().get("fails") or {}).get("used_totp") or [])

    def remember_totp(self, counter: int) -> None:
        """记下已用过的步数。只留最近 20 个，文件不会无限长。"""
        with _LOCK:
            d = self._read()
            f = dict(d.get("fails") or {})
            used = list(f.get("used_totp") or [])
            used.append(int(counter))
            f["used_totp"] = used[-20:]
            d["fails"] = f
            self._write(d)

    # -- 会话 ---------------------------------------------------------------
    def new_session(self, ua: str = "") -> str:
        """签发一个新 token（返回明文，**只此一次**）。顺带清理过期会话。"""
        tok = secrets.token_urlsafe(32)
        with _LOCK:
            d = self._read()
            s = dict(d.get("sessions") or {})
            now = time.time()
            s = {k: v for k, v in s.items() if float((v or {}).get("exp") or 0) > now}
            s[_token_hash(tok)] = {"exp": now + SESSION_TTL,
                                   "ua": (ua or "")[:120]}
            d["sessions"] = s
            self._write(d)
        return tok

    def resolve_session(self, tok: Optional[str]) -> bool:
        """这个 token 现在有效吗？**固定绝对过期，不滑动续期。**"""
        if not tok:
            return False
        with _LOCK:
            d = self._read()
            rec = (d.get("sessions") or {}).get(_token_hash(tok))
            if not rec:
                return False
            return float(rec.get("exp") or 0) > time.time()

    def drop_session(self, tok: Optional[str]) -> None:
        if not tok:
            return
        with _LOCK:
            d = self._read()
            s = dict(d.get("sessions") or {})
            s.pop(_token_hash(tok), None)
            d["sessions"] = s
            self._write(d)

    def drop_all_sessions(self) -> None:
        """改口令后必须调 —— 否则旧的会话仍然能下单。"""
        with _LOCK:
            d = self._read()
            d["sessions"] = {}
            self._write(d)


# ---------------------------------------------------------------------------
# 一次性登录流程（把"口令对了"和"TOTP 对了"绑成一次尝试）
# ---------------------------------------------------------------------------
class LoginFlow:
    """两步登录之间的暂存区，`token -> {user, exp, ok_pw}`。

    ⚠️ **为什么不直接在校验口令后就发会话**：那等于把二重认证降级成单因子
    （一个拿到口令的人直接调 `?step=2` 就能进）。这里第二步**必须**带着
    第一步发出的临时票据，且票据本身短命。
    """

    #: 从"口令对了"到"TOTP 提交"之间允许的秒数。够人看一眼手机。
    TTL = 120

    def __init__(self) -> None:
        self._pending: Dict[str, dict] = {}
        self._lock = threading.RLock()

    def start(self, user: str) -> str:
        tok = secrets.token_urlsafe(24)
        with self._lock:
            self._gc()
            self._pending[tok] = {"user": user, "exp": time.time() + self.TTL}
        return tok

    def claim(self, tok: str) -> Optional[dict]:
        """取出并**立刻作废**（一次性）。"""
        with self._lock:
            self._gc()
            rec = self._pending.pop(tok or "", None)
        if not rec or rec["exp"] < time.time():
            return None
        return rec

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, v in self._pending.items() if v["exp"] < now]:
            self._pending.pop(k, None)
