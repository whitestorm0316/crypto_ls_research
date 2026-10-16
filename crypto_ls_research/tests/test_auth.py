"""鉴权 / 二重认证 / 会话的守卫测试。

这份测试存在的理由
------------------
这个仓库原本**没有任何鉴权**：控制台绑 127.0.0.1，"只有本机能访问"被当成了安全
边界。但那是**本机进程之间**没有边界 —— 任何本机进程都能 `POST /api/live/execute`
下真钱单。加完鉴权之后，最危险的失败不再是"没实现"，而是**实现漏了一个端点**：
一个没挂上闸的端点等于全部没挂。

所以这里的断言分三层，缺一层都不够：

1. **算法对不对** —— TOTP 拿 RFC 6238 官方向量钉；口令哈希拿"错口令必须拒"钉。
2. **闸门在不在、够不够宽** —— 把**所有** `route_get/route_post` 能命中的端点
   枚举出来，逐个断言未登录时 401。只测一两个端点会漏掉那个真正危险的分支。
3. **失败方向够不够"闭"** —— 读不到配置 / 坏 JSON / 缺字段，都必须当**未授权**，
   而不是"因此放行"。
"""
from __future__ import annotations

import base64
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from webapp import auth                                              # noqa: E402


@pytest.fixture
def store(tmp_path):
    return auth.AuthStore(str(tmp_path / "auth.json"))


# ---------------------------------------------------------------------------
# 1. TOTP 必须和标准一致
# ---------------------------------------------------------------------------
def test_totp_matches_rfc6238_vectors():
    """**RFC 6238 的 6 组官方向量。**

    自己写 TOTP 的风险就是"看起来能出 6 位数字"，但和手机上的验证器算的不是
    同一个码。官方向量是唯一能证伪这件事的东西 —— 而如果算错了，二重认证
    从一开始就是坏的，只是表现为"验证码永远不对"。
    """
    # 官方向量用 ASCII 密钥 b"12345678901234567890"；我们内部用 base32
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")

    def ref8(t: int) -> str:
        import hashlib as h, hmac, struct
        d = hmac.new(b"12345678901234567890", struct.pack(">Q", t // 30),
                     h.sha1).digest()
        o = d[-1] & 0x0F
        code = struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF
        return str(code % 10 ** 8).zfill(8)

    cases = [(59, "94287082"), (1111111109, "07081804"),
             (1111111111, "14050471"), (1234567890, "89005924"),
             (2000000000, "69279037"), (20000000000, "65353130")]
    for t, want8 in cases:
        # 6 位实现 = 标准 8 位码的**后 6 位**（RFC 的取模方式决定的）
        assert auth.totp_at(secret, t) == want8[-6:], (
            f"t={t}: 实得 {auth.totp_at(secret, t)}，标准 8 位码是 {want8}")


def test_totp_accepts_one_step_of_drift_but_not_more():
    """±1 步（30 秒）容忍时钟漂移；±2 步必须拒绝，否则窗口就是 2.5 分钟。"""
    s = auth.new_totp_secret()
    now = 1_800_000_000.0
    base = auth.totp_at(s, now)
    ok, _ = auth.verify_totp(s, base, when=now + 30, used=set())
    assert ok, "±1 步应该接受"
    ok, _ = auth.verify_totp(s, base, when=now + 90, used=set())
    assert not ok, "±3 步不该接受"


def test_totp_rejects_replay():
    """🔴 **同一个码不能用两次。**

    没有这一条，一个被肩窥到/被截的码在它那 30 秒里可以反复使用 ——
    而那正是二重认证唯一要防的窗口。
    """
    s = auth.new_totp_secret()
    used: set = set()
    code = auth.totp_now(s)
    ok, _ = auth.verify_totp(s, code, used=used)
    assert ok, "第一次应该通过"
    ok2, why = auth.verify_totp(s, code, used=used)
    assert not ok2 and "用过" in why, f"重放必须被拒，实得 {ok2}/{why}"


def test_totp_rejects_malformed():
    s = auth.new_totp_secret()
    for bad in ("", "abcdef", "12345", "1234567", "  ", None):
        ok, _ = auth.verify_totp(s, bad, used=set())
        assert not ok, f"{bad!r} 不该通过"


# ---------------------------------------------------------------------------
# 2. 口令
# ---------------------------------------------------------------------------
def test_password_roundtrip_and_rejection():
    h = auth.hash_password("correct horse battery staple")
    assert h.startswith("scrypt$")
    assert auth.verify_password("correct horse battery staple", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("", h)


def test_password_comparison_is_constant_time():
    """⚠️ **必须用 `hmac.compare_digest`，不能用 `==`。**

    这条断言的是**语义**而不是行为：`==` 对正确/错误口令给出的答案和
    `compare_digest` **完全一样**，所以任何"只验 True/False"的测试都抓不到
    把 `compare_digest` 换成 `==` 的改动（变异测试验证过这一点）。

    而 `==` 的比较会在**第一个不同的字节**就返回，于是"口令对不对"变成一条
    可以按字节猜的**时序侧信道**。要钉住它，只能看源码用了哪个比较函数。
    """
    import inspect
    src = inspect.getsource(auth.verify_password)
    assert "compare_digest" in src, (
        "verify_password 里没有 compare_digest —— 换成了会短路的比较，"
        "口令校验变成可测时序")
    assert "== want" not in src and "got ==" not in src, (
        "verify_password 里出现了短路比较")


def test_totp_comparison_is_constant_time():
    """同理：**TOTP 那个码的比较**也不能短路。

    ⚠️ 只断言"源码里有 compare_digest"是**不够**的 —— 文件里有两处用它
    （一处是 `verify_totp` 里的码比较、另一处是 `verify_password`），
    于是把**码比较**那处换成 `==` 之后，函数里仍然"含 compare_digest"，
    断言照样通过（变异测试当场抓出来的漏网）。

    所以要精确到**那一行**：必须是 `compare_digest(totp_at(...), code)`。
    """
    import inspect
    src = inspect.getsource(auth.verify_totp)
    assert "compare_digest(totp_at(" in src.replace(" ", ""), (
        "TOTP 的码比较没有用 compare_digest（换成了会短路的 ==）")


def test_password_hash_is_salted():
    """同一个口令两次哈希必须不同 —— 否则能从哈希看出"两个人用了同一个口令"。"""
    a = auth.hash_password("same-password")
    b = auth.hash_password("same-password")
    assert a != b
    assert auth.verify_password("same-password", a)
    assert auth.verify_password("same-password", b)


def test_broken_hash_string_is_rejected_not_crashed():
    """坏哈希串 → False，**不抛异常**。

    抛异常会被上层当成 500，而一个 500 和 401 在攻击者眼里是两种不同的信号
    （前者说明"这里有个我打坏的东西"）。
    """
    for bad in ("", "garbage", "scrypt$1", "bcrypt$..", None, 123):
        assert auth.verify_password("x", bad) is False, f"{bad!r} 应该返回 False"


# ---------------------------------------------------------------------------
# 3. 闸门：所有端点
# ---------------------------------------------------------------------------
#: `/api/live/*` 里**所有**会被路由命中的路径（含方法）。新增端点时必须补进来。
#: ⚠️ 从代码里抓，不是手抄 —— 手抄的清单会随着代码漂移，而漂移那天没人知道。
def _live_paths() -> list:
    import re
    src = open(os.path.join(ROOT, "webapp", "live_api.py"), encoding="utf-8").read()
    return sorted(set(re.findall(r'path == "(/api/live/[^"]+)"', src)))


def test_live_api_paths_were_actually_found():
    """前提检查：抓不到路径说明正则或文件结构变了，后面的断言会空跑。"""
    paths = _live_paths()
    assert len(paths) >= 20, f"只抓到 {len(paths)} 个 live 路径，可能解析坏了"
    assert "/api/live/execute" in paths, "没抓到 execute —— 最危险的那个"


def test_every_live_path_requires_auth():
    """🔴 **`/api/live/*` 每一个都必须要求登录。**

    这是这份文件里最重要的一条。加鉴权最容易犯的错是"改了 90% 的端点" ——
    而漏掉的那个可能正好是下单。断言是逐端点的，所以漏一个就红一个。
    """
    from webapp import server as srv

    # 造一个假 Handler，只借它的 _needs_auth（不需要真的起 socket）
    class Fake:
        _needs_auth = srv.Handler._needs_auth

    f = Fake()
    missing = [p for p in _live_paths() if not f._needs_auth(p)]
    assert not missing, (
        f"这些 /api/live 端点**不需要登录**：{missing} —— "
        f"任何一个都可能是下单/读密钥")


def test_dangerous_paths_are_not_public():
    """点名几个一旦放行就等于没做鉴权的路径。"""
    from webapp import server as srv

    class Fake:
        _needs_auth = srv.Handler._needs_auth

    f = Fake()
    for p in ("/api/live/execute", "/api/live/plan", "/api/live/flatten",
              "/api/live/creds", "/api/live/creds/delete", "/api/live/kill",
              "/api/live/auto/enable", "/api/live/limits", "/api/live/reset",
              "/api/spec", "/api/run"):
        assert f._needs_auth(p), f"{p} 竟然不需要登录"


def test_auth_endpoints_are_public():
    """登录流程本身必须放行，否则没人能登进来（一个把自己锁在门外的设计）。"""
    from webapp import server as srv

    class Fake:
        _needs_auth = srv.Handler._needs_auth

    f = Fake()
    for p in ("/api/auth/status", "/api/auth/setup", "/api/auth/login",
              "/api/auth/verify", "/api/auth/logout"):
        assert not f._needs_auth(p), f"{p} 被挡了 —— 登录流程走不通"


def test_public_set_is_a_whitelist_not_a_blacklist():
    """新增一个没人见过的端点，**默认是"要登录"**。

    这条钉的是方向：白名单漏加只会让页面暂时登不上；黑名单漏加是一个静默的、
    能下单的洞。两者的代价不对称，所以默认必须是"拒绝"。
    """
    from webapp import server as srv

    class Fake:
        _needs_auth = srv.Handler._needs_auth

    f = Fake()
    for new in ("/api/brand-new-endpoint", "/api/live/some-future-action",
                "/api/live2/whatever"):
        assert f._needs_auth(new), f"未知端点 {new} 默认放行了 —— 方向反了"


# ---------------------------------------------------------------------------
# 4. 会话
# ---------------------------------------------------------------------------
def test_session_roundtrip_and_expiry(store):
    store.create_user("u", "a-long-enough-pw")
    tok = store.new_session("ua")
    assert store.resolve_session(tok) is True
    assert store.resolve_session("bogus") is False
    assert store.resolve_session(None) is False
    assert store.resolve_session("") is False


def test_session_stores_only_the_hash(store):
    """落盘的是 token 的**哈希** —— 文件被读走也拿不到能用的 token。"""
    store.create_user("u", "a-long-enough-pw")
    tok = store.new_session("ua")
    raw = open(store.path, encoding="utf-8").read()
    assert tok not in raw, "明文 token 出现在 auth.json 里了"
    assert auth._token_hash(tok) in raw


def test_logout_invalidates(store):
    store.create_user("u", "a-long-enough-pw")
    tok = store.new_session("ua")
    store.drop_session(tok)
    assert store.resolve_session(tok) is False


def test_drop_all_sessions_on_password_change(store):
    """改口令必须能让所有旧会话失效，否则"我改了口令"是句假话。"""
    store.create_user("u", "a-long-enough-pw")
    a = store.new_session("x")
    b = store.new_session("y")
    store.drop_all_sessions()
    assert not store.resolve_session(a)
    assert not store.resolve_session(b)


def test_session_is_not_sliding(store):
    """**固定绝对过期**，不做滑动续期。

    滑动续期 = "只要一直在用就永不过期"，等价于把有效期设成无限 ——
    而这台机器上能下真钱单。
    """
    store.create_user("u", "a-long-enough-pw")
    tok = store.new_session("ua")
    d = json.loads(open(store.path, encoding="utf-8").read())
    exp1 = d["sessions"][auth._token_hash(tok)]["exp"]
    # 解析一次不该推迟过期时间
    store.resolve_session(tok)
    d2 = json.loads(open(store.path, encoding="utf-8").read())
    assert d2["sessions"][auth._token_hash(tok)]["exp"] == exp1, (
        "会话到期时间被续期了 —— 变成滑动过期")


def test_expired_session_is_rejected(store, monkeypatch):
    store.create_user("u", "a-long-enough-pw")
    tok = store.new_session("ua")
    monkeypatch.setattr(auth, "SESSION_TTL", -1)   # 立刻过期
    tok2 = store.new_session("ua")
    assert store.resolve_session(tok2) is False
    assert store.resolve_session(tok) is True     # 早先那个还没到期


# ---------------------------------------------------------------------------
# 5. 失败方向必须"闭"
# ---------------------------------------------------------------------------
def test_read_failure_means_not_configured(tmp_path):
    """读不到文件 ⇒ "没配好"，**不是**"放行"。"""
    s = auth.AuthStore(str(tmp_path / "nope.json"))
    assert s.is_configured() is False
    assert s.user() == {}
    assert s.resolve_session("anything") is False


def test_corrupt_config_means_not_configured(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("{ this is not json", encoding="utf-8")
    s = auth.AuthStore(str(p))
    assert s.is_configured() is False
    assert s.resolve_session("x") is False


def test_config_missing_fields_means_not_configured(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"user": {"name": "u"}}), encoding="utf-8")
    s = auth.AuthStore(str(p))
    assert s.is_configured() is False, "只有用户名、没有口令/totp 也算没配好"


def test_json_of_the_wrong_type_means_not_configured(tmp_path):
    """⚠️ 文件是**合法 JSON 但不是对象**（数组/字符串/数字）。

    这条是补出来的：`_read()` 有两条"读不到"的路径 ——
      * `except` 捕获的 OSError/ValueError（文件不存在、坏 JSON）
      * `else {}`  处理的**类型不对**（这是个 list）
    我原来只测了前者，于是把后者的兜底改成"返回一个假账号"时**测试仍然全绿**
    （变异测试当场抓出来的漏网）。两条路径要各有一条用例。
    """
    for bad in ([1, 2, 3], "just a string", 42, None, True):
        p = tmp_path / "auth.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        s = auth.AuthStore(str(p))
        assert s.is_configured() is False, f"{bad!r} 应被当成「没配好」"
        assert s.user() == {}, f"{bad!r} 竟然读出了用户"


def test_create_user_refuses_to_overwrite(store):
    """已存在就抛 —— 静默覆盖会**悄悄换掉**已有账号的验证器。"""
    store.create_user("u", "a-long-enough-pw")
    with pytest.raises(ValueError):
        store.create_user("u2", "another-long-pw")


def test_create_user_enforces_minimum_length(store):
    with pytest.raises(ValueError):
        store.create_user("u", "short")
    with pytest.raises(ValueError):
        store.create_user("", "a-long-enough-pw")


def test_auth_file_is_not_world_readable(store):
    """口令哈希也不该给别人看。"""
    store.create_user("u", "a-long-enough-pw")
    mode = os.stat(store.path).st_mode & 0o777
    assert mode == 0o600, f"auth.json 权限是 {oct(mode)}，应为 0o600"


# ---------------------------------------------------------------------------
# 6. 节流
# ---------------------------------------------------------------------------
def test_lockout_after_repeated_failures(store):
    store.create_user("u", "a-long-enough-pw")
    for _ in range(auth.MAX_FAILS):
        store.note_fail()
    assert store.locked_for() > 0, "连续失败到阈值却没锁"
    store.note_ok()
    assert store.locked_for() == 0, "成功后应该解锁"


# ---------------------------------------------------------------------------
# 7. 两步登录之间的票据
# ---------------------------------------------------------------------------
def test_login_ticket_is_single_use():
    """票据**一次**用完即废。

    否则第二步可以被反复重放 —— 一个拿到了口令的人可以慢慢试 TOTP 码。
    """
    flow = auth.LoginFlow()
    t = flow.start("u")
    assert flow.claim(t) is not None
    assert flow.claim(t) is None, "票据被复用了"


def test_login_ticket_expires():
    flow = auth.LoginFlow()
    flow.TTL = -1
    t = flow.start("u")
    assert flow.claim(t) is None, "过期票据仍然有效"


def test_unknown_ticket_is_rejected():
    flow = auth.LoginFlow()
    assert flow.claim("nope") is None
    assert flow.claim("") is None
    assert flow.claim(None) is None


# ---------------------------------------------------------------------------
# 8. 外网绑定必须带 TLS
# ---------------------------------------------------------------------------
def test_plaintext_offlocalhost_bind_is_refused(monkeypatch):
    """🔴 **明文绑非本机 ⇒ 拒绝启动。**

    口令和 TOTP 码都要经这条连接传，而登进来能下真钱单。明文在局域网上是
    直接可读的。一个"警告后继续"拦不住任何人。

    这里跑**子进程**，因为它会 `sys.exit`（也正因为会 exit，说明它是硬拒）。
    """
    import subprocess
    env = dict(os.environ)
    env.update({"LS_HOST": "0.0.0.0"})
    env.pop("LS_TLS_CERT", None)
    env.pop("LS_TLS_KEY", None)
    r = subprocess.run([sys.executable, "-m", "webapp.server", "8799"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60,
                       env=env)
    out = r.stdout + r.stderr
    assert "拒绝启动" in out, f"明文外网绑定没被拒绝：{out[-400:]}"
    assert "TLS" in out, "拒绝理由里没说清要 TLS"


def test_localhost_plaintext_is_allowed(monkeypatch):
    """反向断言：**本机明文是允许的**。

    没有这一条，一个"永远拒绝启动"的实现也会让上面那条通过 —— 那就从
    "不安全"变成"根本用不了"。
    """
    from webapp import server as srv
    monkeypatch.setenv("LS_HOST", "127.0.0.1")
    monkeypatch.delenv("LS_TLS_CERT", raising=False)
    monkeypatch.delenv("LS_TLS_KEY", raising=False)
    host, ctx = srv._resolve_bind(8790)
    assert host == "127.0.0.1" and ctx is None


def test_tls_binding_loads_the_cert():
    """给了证书就真的建 TLS 上下文（否则 `Secure` cookie 永远不加）。"""
    import ssl
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        r = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "1", "-keyout", key, "-out", cert,
             "-subj", "/CN=localhost"],
            capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip("openssl 不可用")
        from webapp import server as srv
        env = {"LS_HOST": "0.0.0.0", "LS_TLS_CERT": cert, "LS_TLS_KEY": key}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            host, ctx = srv._resolve_bind(8790)
            assert isinstance(ctx, ssl.SSLContext)
            assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2, (
                "允许了 TLS 1.1 及更早 —— 那些版本已经不安全")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ---------------------------------------------------------------------------
# 9. 凭据文件必须落在仓库**里面**
# ---------------------------------------------------------------------------
def test_auth_file_lives_inside_the_repo():
    """🔴 `AUTH_FILE` 必须在仓库根之下。

    这条是补出来的，因为它真的错了：`auth.py` 的 `ROOT` 是从
    `crypto_ls_research/execution/credentials.py` **抄**的，而那个文件在根下
    **两层**（模式是 `dirname(dirname(__file__))` + `..`）。`webapp/auth.py`
    只在根下**一层**，照搬就多爬了一级 ⇒ 凭据落到 `~/code/config/auth.json`
    —— 仓库**外面**。

    后果三条，每条都很难查：
      1. `.gitignore` 里的 `config/auth.json` **管不到**它：真实凭据在仓库外，
         而且不在任何人的忽略清单里；
      2. 删掉仓库里的 `config/auth.json` 也删不掉账号（服务照旧 configured）；
      3. 换个 clone/换台机器，凭据"凭空消失"，表现为"登录不上"而毫无线索。
    """
    from webapp import auth as a
    repo = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", ".."))
    got = os.path.abspath(a.AUTH_FILE)
    assert got.startswith(repo + os.sep), (
        f"AUTH_FILE 跑到仓库外面了：{got}\n"
        f"  仓库根是 {repo}\n"
        f"  ⇒ .gitignore 的 config/auth.json 管不到它，凭据会裸奔")
    assert a.ROOT == repo, f"ROOT 算错了：{a.ROOT}，应为 {repo}"


def test_auth_file_is_gitignored():
    """凭据文件必须被 git 忽略。

    这条**读 .gitignore 的实际规则**，而不是"看里面有没有那行字" ——
    只有前者能防住"规则写了但被后面的例外覆盖掉"（这个仓库的 `config/`
    确实有一条例外规则，所以这两条都必要）。
    """
    import subprocess
    from webapp import auth as a
    # cwd 必须是仓库根（git 要在那里找 .git）；用 `-q` 只看退出码。
    r = subprocess.run(["git", "check-ignore", "-q", a.AUTH_FILE],
                       cwd=a.ROOT)
    if r.returncode not in (0, 1):
        pytest.skip(f"git 不可用（退出码 {r.returncode}）")
    assert r.returncode == 0, (
        f"{a.AUTH_FILE} **没有**被 git 忽略 —— 口令哈希和 TOTP 密钥会进仓库")


# ---------------------------------------------------------------------------
# 10. 登录页自己必须能加载（白名单不能和 index.html 漂移）
# ---------------------------------------------------------------------------
def test_every_asset_index_html_references_is_public():
    """🔴 **从 `index.html` 里抓引用，逐个断言放行。**

    这条是补出来的，因为真的错了：`_PUBLIC_STATIC` 我手写成
    `("/", "/index.html", "/app.js", "/favicon.ico", "/style.css")`，
    而 `index.html` 里写的是 **`/static/app.js`** 和 **`/static/style.css`**
    ⇒ 登录页的 CSS/JS 全被 401 挡掉 ⇒ 浏览器里只剩一具没有样式的空壳。

    ⚠️ 所以**不要**在测试里再抄一份路径清单（那份也会漂移）—— 直接从
    `index.html` 里**抓** `src=` / `href=`，这才是唯一来源。
    """
    import re
    from webapp import server as srv

    html = open(os.path.join(ROOT, "webapp", "static", "index.html"),
                encoding="utf-8").read()
    refs = re.findall(r'(?:src|href)="(/[^"]+)"', html)
    assert refs, "index.html 里没抓到任何 src/href —— 正则或文件结构变了"

    class Fake:
        _needs_auth = srv.Handler._needs_auth

    f = Fake()
    blocked = [r for r in refs if f._needs_auth(r)]
    assert not blocked, (
        f"这些是 index.html 自己要加载的资源，却被要求登录：{blocked}\n"
        f"  ⇒ 页面会加载成一具没有样式/脚本的空壳。")


def test_public_static_uses_prefix_not_a_file_list():
    """静态资源白名单用**前缀**，不是逐个文件名。

    逐个列举就会和页面实际请求的路径漂移 —— 那正是上面那条测试的成因。
    """
    from webapp import server as srv
    assert any(p.startswith("/static/") for p in srv._PUBLIC_STATIC_PREFIX), (
        "没有 /static/ 前缀规则 —— 又退回到逐条列举文件名了")


def test_static_prefix_does_not_swallow_api():
    """`/static/` 前缀不能顺手把 `/api/` 也放行了。"""
    from webapp import server as srv

    class Fake:
        _needs_auth = srv.Handler._needs_auth

    f = Fake()
    for p in ("/api/live/execute", "/api/live/creds", "/api/spec"):
        assert f._needs_auth(p), f"{p} 被放行了"


# ---------------------------------------------------------------------------
# 11. 首次设置页必须真的能加载（静态资源在任何状态下都放行）
# ---------------------------------------------------------------------------
def test_static_assets_are_public_before_an_account_exists(monkeypatch, tmp_path):
    """🔴 **还没建账号时，静态资源也必须放行。**

    这条是补出来的，因为真的错了，而且是"看不到创建账号按钮"的直接原因：
    `_gate()` 里 `not AUTH.is_configured()` 那一支**自己又写了一遍白名单判断**
    （`path in _PUBLIC_STATIC`），只认文件名、不认 `/static/` 前缀 ⇒
    首次使用时 `/static/app.js` 回 401 ⇒ 页面 JS 加载不出来 ⇒ 屏幕上只有
    一具空壳，**连"创建账号"按钮都没有**。

    ⚠️ 同一条规则写两遍，两遍就会不一致 —— 这条测试钉的是"两个分支共用
    `_needs_auth` 这一个定义"。
    """
    from webapp import server as srv
    from webapp import auth as auth_mod

    # 造一个"还没建账号"的 store
    monkeypatch.setattr(srv, "AUTH", auth_mod.AuthStore(str(tmp_path / "none.json")))
    assert srv.AUTH.is_configured() is False

    class Fake:
        path = ""
        _err = lambda self, *a, **k: None            # noqa: E731
        _needs_auth = srv.Handler._needs_auth
        _gate = srv.Handler._gate

    f = Fake()
    blocked = []
    for p in ("/", "/index.html", "/static/app.js", "/static/style.css"):
        f.path = p
        if not f._gate():
            blocked.append(p)
    assert not blocked, (
        f"还没建账号时这些静态资源被挡了：{blocked}\n"
        f"  ⇒ 首次设置页的 JS/CSS 加载不出来，用户**看不到「创建账号」按钮**")

    # 反向：这时候业务接口仍然必须挡（别顺手把门全开了）
    for p in ("/api/spec", "/api/live/execute"):
        f.path = p
        assert not f._gate(), f"{p} 在未建账号时被放行了"


def test_gate_does_not_redefine_the_whitelist():
    """`_gate` 里不许再出现第二份白名单判断。

    这条盯**成因**：只要 `_gate` 里出现 `_PUBLIC_STATIC` 的字样，就说明有人
    又把那条规则抄了一遍 —— 而两份定义迟早会不一致。
    """
    import inspect
    from webapp import server as srv
    src = inspect.getsource(srv.Handler._gate)
    # ⚠️ 只看**非注释行**：`_gate` 的注释里会提到 `_PUBLIC_STATIC`（解释它
    # 为什么不该出现在这里），扫全文本会命中那段说明 —— 第一版就是这么误报的。
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(code)
    assert "_PUBLIC_STATIC" not in body, (
        "_gate 里又出现了 _PUBLIC_STATIC —— 白名单被写了两遍，"
        "将来一定会有一次只改了一处")
    assert "_needs_auth" in body, "_gate 应该问 _needs_auth，而不是自己判断路径"
