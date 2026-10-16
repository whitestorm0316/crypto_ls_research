"""控制台的 HTTP 契约：`/api/*` 的错误必须是 **JSON**，不能是 HTML 错误页。

## 为什么要有这个文件

`BaseHTTPRequestHandler` 对**没有实现的方法**会走
`handle_one_request` -> `send_error(501, "Unsupported method (...)")`，
回一张 `Content-Type: text/html` 的错误页。

前端 `deleteRun()` 当时写的是 `resp.json()`，于是在这上面抛：

    Unexpected token '<', "<!DOCTYPE "... is not valid JSON

用户看到的就是这一句 —— **真正的原因（服务端根本不支持 DELETE）一个字都没露出来**。
实测复现（2026-09-30）：

* 控制台进程 PID 9982 是 **09-29 18:51** 起的，
* 而 `do_DELETE` 是 **09-30 00:46** 才进仓库的（commit `321a31d`）。

改过 `webapp/` 却没重启 -> 进程里还是旧代码 -> `DELETE /api/runs/<id>` 回
`501 Unsupported method ('DELETE')` + HTML -> 删除按钮报「JSON 解析失败」。

所以这里钉两条：

1. **服务端**：`Handler` 必须实现 `do_DELETE`，且 `/api/` 下**任何**错误
   （包括没实现的方法）都必须回 JSON。
2. **前端**：`deleteRun()` 不允许在没确认 `ok` / 可解析之前直接 `.json()` ——
   否则同一个错会以「JSON 解析失败」的面目再出现一次。

第 1 条用**真起一个服务**来测，不是读源码：契约是「线上回什么」，不是「源码里写了什么」。
"""

from __future__ import annotations

import json
import re
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from webapp.server import Handler

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 真起一个服务，用裸 socket 打请求。
# 不用 `urllib`/`curl`：这个沙箱里 `curl` 的 127.0.0.1 会被代理接管，服务没起也回
# 502；而我们要看的正是**响应体到底是什么**，所以自己拼 HTTP/1.1。
# ---------------------------------------------------------------------------
@pytest.fixture()
def console(monkeypatch, tmp_path):
    """真起一个服务。

    ⚠️ 2026-10-16 起服务端**默认拒绝一切 /api/**，所以这个 fixture 必须
    先"注册 + 登录"拿到会话，否则**每个**断言都会收到 401 而不是它想测的东西
    （三个测试就是这么开始失败的）。

    这里刻意**真的走一遍登录流程**，而不是伪造一个 session token 塞进 store ——
    伪造的话，"登录链路坏了"这件事在这份测试里就永远看不出来。
    """
    from webapp import auth as auth_mod
    monkeypatch.setattr(auth_mod, "AUTH_FILE", str(tmp_path / "auth.json"))
    # 模块级的 AUTH/LOGIN 绑定在 import 时就建好了，指向真实路径 —— 换掉它们。
    import webapp.server as srv_mod
    monkeypatch.setattr(srv_mod, "AUTH", auth_mod.AuthStore(str(tmp_path / "auth.json")))
    monkeypatch.setattr(srv_mod, "LOGIN", auth_mod.LoginFlow())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()


def _raw(port: int, method: str, path: str, body: dict | None = None,
         cookie: str | None = None) -> tuple[int, dict, str]:
    """Returns (status, headers, body).  Bare socket on purpose."""
    payload = json.dumps(body).encode() if body is not None else b""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    head = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}",
            "Accept: */*", "Connection: close"]
    if cookie:
        head.append(f"Cookie: {cookie}")
    if body is not None:
        head.append("Content-Type: application/json")
        head.append(f"Content-Length: {len(payload)}")
    s.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
    buf = b""
    while True:
        try:
            chunk = s.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    s.close()
    text = buf.decode("utf-8", "replace")
    head_txt, _, body = text.partition("\r\n\r\n")
    lines = head_txt.splitlines()
    status = int(lines[0].split()[1]) if lines else 0
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, body


@pytest.fixture()
def authed(console):
    """`(port, cookie)` —— 走完真实的注册 + 两步登录。"""
    from webapp import auth as auth_mod

    st, _, body = _raw(console, "POST", "/api/auth/setup",
                       {"user": "t", "password": "a-long-enough-pw"})
    assert st == 200, f"注册失败：{st} {body[:200]}"
    secret = json.loads(body)["secret"]

    st, _, body = _raw(console, "POST", "/api/auth/login",
                       {"user": "t", "password": "a-long-enough-pw"})
    assert st == 200, f"登录第一步失败：{st} {body[:200]}"
    ticket = json.loads(body)["ticket"]

    code = auth_mod.totp_now(secret)
    st, headers, body = _raw(console, "POST", "/api/auth/verify",
                             {"ticket": ticket, "code": code})
    assert st == 200, f"登录第二步失败：{st} {body[:200]}"
    sc = headers.get("set-cookie", "")
    m = re.search(r"ls_session=[^;]+", sc)
    assert m, f"没拿到会话 cookie：{sc!r}"
    return console, m.group(0)


def test_the_handler_implements_delete():
    """The method that was missing from the running process.

    A stale console is not something a unit test can see, but "the source no
    longer has `do_DELETE`" is -- and that is the change that would make the
    restart pointless.
    """
    assert hasattr(Handler, "do_DELETE"), (
        "Handler 没有 do_DELETE：删除运行记录会 501 + HTML，前端报"
        "「JSON 解析失败」而不是真正的原因")


def test_delete_on_a_missing_run_returns_json(authed):
    console, cookie = authed
    status, headers, body = _raw(console, "DELETE", "/api/runs/__no_such_run__",
                                 cookie=cookie)
    assert headers.get("content-type", "").startswith("application/json"), (
        f"DELETE 的错误不是 JSON：{headers.get('content-type')!r} / {body[:200]}")
    assert status == 404
    assert "no such run" in json.loads(body)["error"]


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH", "OPTIONS"])
def test_unimplemented_methods_on_api_paths_return_json(authed, method):
    """The 501 branch -- the one that produced `<!DOCTYPE`.

    Only `/api/` paths are converted; static/page errors stay HTML because a
    human opening that URL in a browser wants a readable page.
    """
    console, cookie = authed
    status, headers, body = _raw(console, method, "/api/definitely-not-a-route",
                                 cookie=cookie)
    assert headers.get("content-type", "").startswith("application/json"), (
        f"{method} /api/... 回了 {headers.get('content-type')!r}，"
        f"前端 resp.json() 会抛「JSON 解析失败」：{body[:200]}")
    assert "<!DOCTYPE" not in body and "<html" not in body.lower()
    json.loads(body)          # 必须是能解析的 JSON
    assert status in (404, 501)


def test_the_json_override_is_scoped_to_api_paths(console):
    """反向断言：`send_error` 的覆盖**只**作用于 `/api/`。

    没有这条，把 `send_error` 写成「一律 JSON」也能过上面的测试 ——
    而那样浏览器直接打开一个坏链接时只会看到一坨 JSON，不是人看的错误页。

    注意用 `PUT`（没实现的方法）而不是 `GET`：`do_GET` 自己就会对未知路径回
    JSON 404（这是**既有**行为，不是这次改的），所以 `GET` 测不出覆盖范围。
    只有走到 `send_error` 的那条路（未实现的方法）才能分辨父类的 HTML 与我们的 JSON。
    """
    status, headers, body = _raw(console, "PUT", "/no-such-file-xyz.html")
    assert status == 501, f"预期未实现的方法回 501，实际 {status}"
    assert headers.get("content-type", "").startswith("text/html"), (
        f"非 /api/ 路径不该被改成 JSON：{headers.get('content-type')!r}")
    assert "<!DOCTYPE" in body


def test_an_unknown_get_path_is_still_a_json_404(authed):
    """既有行为，钉住它：`do_GET` 对未知路径回 JSON 404（不是 HTML）。

    这跟上面那条一起说明覆盖范围：`/api/` **和** `do_GET` 的兜底是 JSON，
    其余（未实现的方法）留给父类的 HTML。
    """
    console, cookie = authed
    status, headers, body = _raw(console, "GET", "/no-such-file-xyz.html",
                                 cookie=cookie)
    assert status == 404
    assert headers.get("content-type", "").startswith("application/json")
    assert json.loads(body)["error"]


def test_the_frontend_never_blindly_parses_a_response():
    """`deleteRun()` 必须先看 `resp.ok`，再决定怎么解释响应体。

    这是同一个错的另一半：服务端回 HTML 时，前端至少要说出
    「HTTP 501 + 不是 JSON」，而不是把 `Unexpected token '<'` 丢给用户。
    """
    src = (ROOT / "webapp" / "static" / "app.js").read_text()
    i = src.find("async function deleteRun(")
    assert i > 0, "app.js 里找不到 deleteRun()"
    j = src.find("\n}\n", i)
    body = src[i:j]

    assert "await resp.text()" in body, (
        "deleteRun 没有先取 text：非 JSON 响应会变成一句 'Unexpected token <'")
    assert "JSON.parse(raw)" in body, "deleteRun 没有自己解析，无法区分「不是 JSON」"
    assert re.search(r"resp\.status === 501", body), (
        "deleteRun 没有点名 501：用户看不到「控制台没重启」这个真正的原因")
    assert not re.search(r"await\s+resp\.json\(\)", body), (
        "deleteRun 仍在直接 resp.json()：同一个错会再出现一次")


def test_the_delete_button_still_targets_the_runs_route():
    """相邻对断言：改的是**错误处理**，不是路由。

    上面那条只要求「不再盲目 .json()」；如果不钉住 URL 与 method，
    把 fetch 整个删掉也能过 —— 而删除就真的坏了。
    """
    src = (ROOT / "webapp" / "static" / "app.js").read_text()
    i = src.find("async function deleteRun(")
    body = src[i:src.find("\n}\n", i)]
    assert re.search(r"fetch\('/api/runs/'\s*\+\s*encodeURIComponent\(id\)", body)
    assert re.search(r"method:\s*'DELETE'", body)


# ---------------------------------------------------------------------------
# 鉴权发生在**路由之前**（2026-10-16）
# ---------------------------------------------------------------------------
def test_unauthenticated_gets_401_not_404(console):
    """🔴 未登录时，**连"这个路径存不存在"都不该告诉对方**。

    这里要钉的是顺序：闸门在路由**之前**。如果先路由再鉴权，那么
    "不存在的路径回 404、存在的回 401"就成了一条**枚举端点**的通道 ——
    攻击者可以据此摸清有哪些接口，再一个个试。

    所以未登录时，存在与不存在的路径必须回**同一个** 401。
    """
    exist = _raw(console, "GET", "/api/live/status?mode=paper")
    missing = _raw(console, "GET", "/api/no-such-endpoint-at-all")
    assert exist[0] == 401, f"已有端点未登录竟然回 {exist[0]}"
    assert missing[0] == 401, (
        f"不存在的端点回了 {missing[0]}，而已有的是 401 —— "
        f"两个码不同就等于告诉对方哪些端点存在")
    # 响应体也不该泄露"这个路径不存在"
    assert "not found" not in missing[2], "未登录时泄露了路由信息"


def test_unauthenticated_post_cannot_reach_the_handler(console):
    """未登录的 POST 必须在**解析 body 之前**就被挡掉。

    否则一个畸形 body 会让服务端走进解析分支 —— 那是不该给匿名请求的
    处理机会，而且它的报错（400 vs 401）又是一条信息。
    """
    st_raw, _, body = _raw(console, "POST", "/api/live/execute", {"anything": 1})
    assert st_raw == 401, f"未登录的 POST 回了 {st_raw}"
    assert "未登录" in body or "尚未创建账号" in body
