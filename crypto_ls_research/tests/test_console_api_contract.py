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
def console():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()


def _raw(port: int, method: str, path: str) -> tuple[int, dict, str]:
    """Returns (status, headers, body).  Bare socket on purpose."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    head = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}",
            "Accept: */*", "Connection: close"]
    s.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
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


def test_the_handler_implements_delete():
    """The method that was missing from the running process.

    A stale console is not something a unit test can see, but "the source no
    longer has `do_DELETE`" is -- and that is the change that would make the
    restart pointless.
    """
    assert hasattr(Handler, "do_DELETE"), (
        "Handler 没有 do_DELETE：删除运行记录会 501 + HTML，前端报"
        "「JSON 解析失败」而不是真正的原因")


def test_delete_on_a_missing_run_returns_json(console):
    status, headers, body = _raw(console, "DELETE", "/api/runs/__no_such_run__")
    assert headers.get("content-type", "").startswith("application/json"), (
        f"DELETE 的错误不是 JSON：{headers.get('content-type')!r} / {body[:200]}")
    assert status == 404
    assert "no such run" in json.loads(body)["error"]


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH", "OPTIONS"])
def test_unimplemented_methods_on_api_paths_return_json(console, method):
    """The 501 branch -- the one that produced `<!DOCTYPE`.

    Only `/api/` paths are converted; static/page errors stay HTML because a
    human opening that URL in a browser wants a readable page.
    """
    status, headers, body = _raw(console, method, "/api/definitely-not-a-route")
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


def test_an_unknown_get_path_is_still_a_json_404(console):
    """既有行为，钉住它：`do_GET` 对未知路径回 JSON 404（不是 HTML）。

    这跟上面那条一起说明覆盖范围：`/api/` **和** `do_GET` 的兜底是 JSON，
    其余（未实现的方法）留给父类的 HTML。
    """
    status, headers, body = _raw(console, "GET", "/no-such-file-xyz.html")
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
