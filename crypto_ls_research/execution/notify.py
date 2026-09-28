"""Out-of-band notification: tell a human when the desk actually does something.

Why this is a plain HTTP POST and not a connector call
-----------------------------------------------------
The unattended rebalancer (`execution.auto_trader`) is a **standalone daemon**,
not an agent turn.  It has no access to the WorkBuddy tool surface, so it cannot
invoke an MCP connector.  What it can do is an HTTP POST -- which is exactly how
every "push to my phone" service works, including 飞书自定义机器人, 企业微信群机器人,
Server酱 and PushPlus.  So the daemon carries its own notifier, configured from
`config/notify.json`.

Four rules this module obeys
----------------------------
1. **A notification must never affect trading.**  Nothing here raises into the
   order path: every failure comes back as `{"ok": False, "error": ...}`.  A dead
   webhook is an annoyance; an exception thrown mid-rebalance is a half-sent book.
2. **Silence is the default for routine ticks.**  "Not a rebalance day" happens
   ~24 times a day; pushing that is how a person learns to ignore the channel.
   Notify on *state changes* -- traded / blocked / error -- and de-duplicate a
   repeating failure so a broken hour does not become 24 messages.
3. **Never log the credential.**  Every message and error string is redacted
   before it is written anywhere.  Note that the credential is *not* always a
   query parameter -- 飞书 and Server酱 put it in the path -- so `redact()` masks
   both, or the token ends up in plaintext in a world-readable log.  See
   `redact()`.
4. **HTTP 200 is not a verdict.**  Every one of these services answers a
   *rejected* push with 200 and an error in the body -- a rotated webhook
   (`errcode 93000`), a failed signature (`code 19021`), an unmatched security
   keyword (`code 19024`), an invalid token.  Reporting those as success is
   worse than having no notification at all, because silence then reads as
   "no rebalance happened".  This is the same two-layer envelope as OKX's batch
   endpoint, where the envelope `code` describes the request and the per-row
   `sCode` describes the order.  See `_verdict()`.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))
CONFIG_PATH = os.path.join(ROOT, "config", "notify.json")
EXAMPLE_PATH = os.path.join(ROOT, "config", "notify.example.json")

#: Which outcomes are worth a push.  `not_due` / `noop` are deliberately absent:
#: they are the normal case, and pushing the normal case trains people to ignore
#: the channel.
DEFAULT_NOTIFY_ON = ("traded", "blocked", "error")

PROVIDERS = ("feishu", "wecom", "serverchan", "pushplus", "webhook", "none")


# ---------------------------------------------------------------------------
#: A webhook's credential is **not** always a query parameter.  飞书 puts it in
#: the path (`…/bot/v2/hook/<id>`) and so does Server酱 (`…/<SENDKEY>.send`), so
#: masking only `?key=` wrote the token in plaintext into `logs/auto_demo.log`
#: and into the heartbeat `artifacts/live/*/auto.json` -- both world-readable.
#: A credential tail is long and carries digits; the words `/hook` and `/send`
#: are neither, which is what keeps a genuinely keyless URL readable.
_SECRET_TAIL = re.compile(r"^[A-Za-z0-9._~-]{12,}$")


def _looks_like_a_token(seg: str) -> bool:
    if not _SECRET_TAIL.match(seg):
        return False
    return any(c.isdigit() for c in seg) or len(seg) >= 20


def redact(url: str) -> str:
    """`…/send?key=SECRET` -> `…/send?***` and `…/hook/<id>` -> `…/hook/***`.

    Both places a credential can hide are masked, because these strings end up
    in a log file that is read, copied and pasted.  This only affects what is
    *displayed*; the transport still receives the real URL.
    """
    if not url:
        return ""
    try:
        p = urllib.parse.urlsplit(str(url))
    except ValueError:
        return "***"
    if not p.netloc:
        return "***"
    segs = p.path.split("/")
    if segs and _looks_like_a_token(segs[-1]):
        segs[-1] = "***"
    out = f"{p.scheme}://{p.netloc}{'/'.join(segs)}"
    return out + "?***" if p.query else out


_TRUNC_SUFFIX = "\n…（内容过长已截断）"


def _truncate(text: str, max_bytes: int) -> str:
    """Cut on a character boundary and keep the **result** within `max_bytes`.

    The suffix is reserved *before* slicing: slicing to `max_bytes` and then
    appending 30 bytes of suffix is how a "truncated" body still comes back over
    the service's limit and gets a 400 instead of a message.
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    keep = max(0, max_bytes - len(_TRUNC_SUFFIX.encode("utf-8")))
    return raw[:keep].decode("utf-8", "ignore") + _TRUNC_SUFFIX


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE = re.compile(r"`([^`]*)`")


def _plain_md(text: str) -> str:
    """Drop emphasis that a provider will not render.

    飞书's `post` message is *not* markdown -- sending `**被拒 1 笔**` shows the
    asterisks.  A 飞书 card would render it, but the 自定义关键词 security filter
    is only documented to apply to text/title params, so a card can be rejected
    with `code 19024` where a post cannot.  On a system that moves money, the
    emphasis goes -- not the delivery guarantee.
    """
    return _MD_CODE.sub(r"\1", _MD_BOLD.sub(r"\1", text))


def _post_json(url: str, payload: dict, timeout: float) -> Tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310
        return r.status, r.read(2000).decode("utf-8", "replace")


def _post_form(url: str, payload: dict, timeout: float) -> Tuple[int, str]:
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310
        return r.status, r.read(2000).decode("utf-8", "replace")


#: How each service spells "accepted" **inside the body**.  Note that no two
#: agree: 飞书/Server酱 use `code: 0`, PushPlus uses `code: 200`, 企业微信 uses
#: `errcode: 0`.  Getting this table wrong in the lenient direction is the bug
#: `_verdict()` exists to prevent.
_BODY_OK = {
    "feishu": ("code", {"0"}),
    "wecom": ("errcode", {"0"}),
    "serverchan": ("code", {"0"}),
    "pushplus": ("code", {"200"}),
}
_BODY_MSG = ("errmsg", "msg", "message", "error")


def _verdict(provider: str, status: Any, body: str) -> Tuple[bool, str]:
    """Turn `(status, body)` into a real verdict.

    HTTP 200 means the request *arrived*, not that the message was sent.  A
    rotated webhook, a bad signature and an unmatched security keyword all come
    back as 200 with the error in the body, so a status-only check reports a
    push that never happened as `ok: True` -- and then a quiet channel reads as
    "the strategy did nothing" instead of "your notifications are broken".

    The generic `webhook` provider is exempt: the peer defines its own body, so
    there is no field we can honestly read a verdict out of.
    """
    try:
        st = int(status)
    except (TypeError, ValueError):
        st = 0
    if not 200 <= st < 300:
        return False, f"HTTP {st}"
    spec = _BODY_OK.get(provider)
    if not spec:
        return True, ""
    try:
        d = json.loads(body) if body else {}
    except ValueError:
        return True, ""        # not JSON -> nothing to contradict the 200
    if not isinstance(d, dict):
        return True, ""
    field, good = spec
    if field not in d:
        return True, ""        # field absent -> do not invent a failure
    if str(d.get(field)) in good:
        return True, ""
    why = next((str(d[k])[:120] for k in _BODY_MSG if d.get(k)), "")
    return False, f"{field}={d.get(field)}" + (f" {why}" if why else "")


def feishu_sign(secret: str, timestamp: str) -> str:
    """飞书's signature, which is not the usual "sign the body" shape.

    The HMAC **key** is `"{timestamp}\\n{secret}"` and the signed message is the
    **empty** byte string -- so `hmac(key=secret, msg=body)` produces a
    signature that fails with `code 19021`.  Copied from the official doc's
    Python sample; do not "fix" it into the conventional form.
    """
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(
        hmac.new(key, b"", digestmod=hashlib.sha256).digest()).decode("ascii")


# ---------------------------------------------------------------------------
class Notifier:
    """Best-effort push.  `send()` never raises."""

    def __init__(self, cfg: Optional[dict] = None, *, mode: Optional[str] = None,
                 post: Optional[Callable] = None,
                 post_form: Optional[Callable] = None, log=None) -> None:
        c = dict(cfg or {})
        self.enabled = bool(c.get("enabled"))
        self.provider = str(c.get("provider") or "none").lower()
        if self.provider not in PROVIDERS:
            raise ValueError(
                f"未知的 provider {self.provider!r}，可选：{'/'.join(PROVIDERS)}")
        self.url = str(c.get("url") or "")
        self.token = str(c.get("token") or "")
        #: 飞书 only: the 签名校验 secret.  Empty means the bot is relying on
        #: the keyword or IP-whitelist security mode instead.
        self.secret = str(c.get("secret") or "")
        #: 飞书 only: the 自定义关键词.  A message without it is rejected with
        #: `code 19024`, so it is prepended rather than hoped for.
        self.keyword = str(c.get("keyword") or "")
        self.notify_on = tuple(c.get("notify_on") or DEFAULT_NOTIFY_ON)
        self.modes = tuple(c.get("modes") or ("paper", "demo", "live"))
        self.timeout = float(c.get("timeout") or 10.0)
        self.max_bytes = int(c.get("max_bytes") or 3800)
        #: Suppress a *repeating* failure.  `traded` is never de-duplicated: it is
        #: rare, it is the thing the user asked to hear about, and every one of
        #: them matters.
        self.dedup_sec = float(c.get("dedup_sec") or 6 * 3600)
        self.mode = mode
        self._post = post
        #: Form posts are injectable too -- otherwise the Server酱 test cannot
        #: observe its payload and silently performs a real HTTP call instead.
        self._post_form = post_form
        self._log = log or (lambda m: None)
        self._recent: Dict[str, float] = {}

    # -- construction ------------------------------------------------------
    @classmethod
    def from_config(cls, *, mode: Optional[str] = None, path: Optional[str] = None,
                    post: Optional[Callable] = None,
                    post_form: Optional[Callable] = None, log=None) -> "Notifier":
        """Load `config/notify.json`.  A missing or broken file = notifier off.

        Deliberately silent and non-fatal: the trading daemon must start whether
        or not anyone remembered to configure notifications.
        """
        p = path or CONFIG_PATH
        cfg: dict = {}
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except FileNotFoundError:
            cfg = {}
        except (OSError, ValueError):
            cfg = {}
        return cls(cfg, mode=mode, post=post, post_form=post_form, log=log)

    def config_path(self) -> str:
        return CONFIG_PATH

    def describe(self) -> dict:
        """What the user needs in order to tell whether it is wired up.

        `secret_set` is a boolean, never the secret: this dict is printed by
        `--show` and lands in logs.
        """
        return {"enabled": self.enabled, "provider": self.provider,
                "url": redact(self.url), "notify_on": list(self.notify_on),
                "modes": list(self.modes), "dedup_sec": self.dedup_sec,
                "secret_set": bool(self.secret),
                "ready": self._ready(),
                "config_path": self.config_path()}

    def _ready(self) -> bool:
        if not self.enabled or self.provider == "none":
            return False
        if self.provider == "pushplus":
            return bool(self.token)
        return bool(self.url)

    # -- policy ------------------------------------------------------------
    def enabled_for(self, action: str, mode: Optional[str] = None) -> bool:
        if not self._ready():
            return False
        m = mode or self.mode
        if m and m not in self.modes:
            return False
        return action in self.notify_on

    def _dedup_ok(self, key: str, action: str) -> bool:
        if action == "traded":
            return True
        now = time.time()
        last = self._recent.get(key)
        if last is not None and now - last < self.dedup_sec:
            return False
        self._recent[key] = now
        return True

    # -- transport ---------------------------------------------------------
    def send(self, title: str, text: str, *, key: str = "", action: str = "") -> dict:
        """Best-effort.  Returns `{"ok": bool, ...}`; **never raises**."""
        if not self._ready():
            return {"ok": False, "skipped": "未配置或未启用", "provider": self.provider}
        if key and not self._dedup_ok(key, action):
            return {"ok": False, "skipped": "重复通知已抑制", "provider": self.provider}
        # Reserve room for the title: every provider carries it alongside the
        # body, so truncating the body to the full budget still overflows.
        budget = max(64, self.max_bytes - len(str(title).encode("utf-8")) - 8)
        text = _truncate(text, budget)
        try:
            return self._dispatch(title, text)
        except urllib.error.HTTPError as e:                     # noqa: PERF203
            return {"ok": False, "provider": self.provider,
                    "error": f"HTTP {e.code} {e.reason}",
                    "url": redact(self.url)}
        except Exception as e:                                  # noqa: BLE001
            return {"ok": False, "provider": self.provider,
                    "error": f"{type(e).__name__}: {e}",
                    "url": redact(self.url)}

    def _feishu_body(self, title: str, text: str) -> dict:
        """飞书自定义机器人 payload.

        `post` rather than `text`: it gives a real title and one paragraph per
        line, so the itemised "被拒 N 笔" list stays a list.  `text` would
        flatten the whole thing into one blob.

        Emphasis is stripped because `post` is not markdown -- see `_plain_md`.
        """
        head = f"{self.keyword} {title}".strip() if self.keyword else title
        body = {
            "msg_type": "post",
            "content": {"post": {"zh_cn": {
                "title": _plain_md(head),
                "content": [[{"tag": "text", "text": _plain_md(ln)}]
                            for ln in text.split("\n")],
            }}},
        }
        if self.secret:
            # 签名校验 is on: 飞书 rejects the message with `code 19021` if the
            # timestamp is more than an hour old, so this must be computed at
            # send time, not at construction time.
            ts = str(int(time.time()))
            body["timestamp"] = ts
            body["sign"] = feishu_sign(self.secret, ts)
        return body

    def _dispatch(self, title: str, text: str) -> dict:
        post_json = self._post or _post_json
        post_form = self._post_form or _post_form
        if self.provider == "feishu":
            st, body = post_json(self.url, self._feishu_body(title, text),
                                 self.timeout)
        elif self.provider == "wecom":
            # 企业微信群机器人.  `markdown` keeps the itemised failure list
            # readable; `text` would flatten it into one blob.
            st, body = post_json(self.url, {
                "msgtype": "markdown",
                "markdown": {"content": f"**{title}**\n{text}"}}, self.timeout)
        elif self.provider == "serverchan":
            st, body = post_form(self.url, {"title": title, "desp": text},
                                 self.timeout)
        elif self.provider == "pushplus":
            st, body = post_json("https://www.pushplus.plus/send", {
                "token": self.token, "title": title, "content": text,
                "template": "markdown"}, self.timeout)
        else:                                                    # generic webhook
            st, body = post_json(self.url, {
                "title": title, "text": text, "mode": self.mode}, self.timeout)

        ok, why = _verdict(self.provider, st, body)
        out = {"ok": ok, "status": int(st), "provider": self.provider,
               "url": redact(self.url), "resp": (body or "")[:200]}
        if why:
            out["error"] = why
        return out


# ---------------------------------------------------------------------------
def _f(x: Any, d: int = 2) -> str:
    try:
        return f"{float(x):,.{d}f}"
    except (TypeError, ValueError):
        return "—"


def _num(x: Any) -> str:
    """Integers for the order counts.  A missing field must render as a dash --
    `f"{None}"` puts the literal `None` into a message a person reads."""
    try:
        return f"{int(x):,}"
    except (TypeError, ValueError):
        return "—"


def _pct(x: Any, d: int = 2) -> str:
    try:
        return f"{float(x) * 100:.{d}f}%"
    except (TypeError, ValueError):
        return "—"


def format_rebalance(out: dict) -> Tuple[str, str]:
    """The push body.  This is the whole point of the feature, so it says what a
    person actually needs to judge the rebalance: what the strategy wanted, what
    went out, what came back, and **why anything was rejected** -- the last one
    is otherwise only visible by opening the console.
    """
    mode = out.get("mode") or "?"
    action = out.get("action") or "?"
    label = {"demo": "模拟盘", "live": "实盘", "paper": "纸面"}.get(mode, mode)
    if action == "traded":
        title = f"✅ 调仓完成 · {label}"
    elif action == "blocked":
        title = f"⛔ 风控拦截，未下单 · {label}"
    elif action == "error":
        title = f"⚠️ 自动交易出错 · {label}"
    else:
        title = f"ℹ️ {action} · {label}"

    lines = []
    if out.get("decision_ts"):
        d = str(out["decision_ts"])[:16].replace("T", " ")
        nxt = str(out.get("next_decision_ts") or "")[:16].replace("T", " ")
        lines.append(f"信号日 {d}" + (f" · 下次 {nxt}" if nxt else ""))
    if out.get("nav") is not None:
        lines.append(f"净值 {_f(out['nav'])} USDT")
    if out.get("target_gross") is not None:
        lines.append(f"目标毛敞口 {_f(out['target_gross'], 3)}× NAV")

    if action == "traded":
        lines.append(f"订单 {_num(out.get('n_orders'))} 笔 → "
                     f"成交 {_num(out.get('n_ok'))} · 失败 {_num(out.get('n_fail'))}")
        if out.get("gross_realised") is not None:
            lines.append(f"实际毛敞口 {_f(out['gross_realised'])} USDT")
        if out.get("turnover_frac") is not None:
            lines.append(f"本次换手 {_pct(out['turnover_frac'])}")
    elif action == "blocked":
        lines.append("**未发送任何订单**")
        for b in (out.get("blocked_by") or []):
            lines.append(f"· 闸门：{b}")
    elif action == "error":
        lines.append(str(out.get("error") or "")[:300])

    if out.get("run_id"):
        lines.append(f"run {out['run_id']}")

    fails = out.get("failures") or []
    if fails:
        lines.append("")
        lines.append(f"**被拒 {len(fails)} 笔**")
        for x in fails[:6]:
            code = x.get("code") or ""
            lines.append(f"· {x.get('instId')}"
                         + (f" `{code}`" if code else "")
                         + f" {str(x.get('error') or '')[:70]}")

    warns = out.get("warnings") or []
    if warns:
        lines.append("")
        lines.append("**警告**")
        for w in warns[:4]:
            lines.append(f"· {str(w)[:80]}")

    return title, "\n".join(lines)


def notify_for(out: dict, notifier: Notifier) -> dict:
    """Send the push for one `AutoTrader.once()` outcome.  Never raises."""
    action = out.get("action") or ""
    if not notifier.enabled_for(action, out.get("mode")):
        return {"ok": False, "skipped": "该结果不推送", "provider": notifier.provider}
    title, text = format_rebalance(out)
    key = f"{out.get('mode')}|{action}|{out.get('reason')}|{str(out.get('error') or '')[:80]}"
    return notifier.send(title, text, key=key, action=action)


# ---------------------------------------------------------------------------
def write_config(patch: dict, path: Optional[str] = None) -> str:
    """Merge `patch` into `config/notify.json`, seeding from the template.

    A person pasting a webhook URL should not also have to get the JSON right:
    a stray comma in that file is indistinguishable from "notifications are
    broken", which is the one failure they cannot diagnose themselves.  Written
    via a temp file + rename so an interrupted write cannot leave a half-file
    behind, and chmod 0600 because the URL carries a key.
    """
    p = path or CONFIG_PATH
    cfg: dict = {}
    for src in (p, EXAMPLE_PATH):
        try:
            with open(src, encoding="utf-8") as f:
                cfg = json.load(f) or {}
            break
        except (OSError, ValueError):
            cfg = {}
    cfg.update(patch)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="调仓通知配置与自检")
    ap.add_argument("--test", action="store_true", help="发一条测试消息")
    ap.add_argument("--show", action="store_true", help="显示当前配置（URL 已打码）")
    ap.add_argument("--set-url", metavar="URL",
                    help="写入 webhook 地址（顺带 enabled:true）并立即自检")
    ap.add_argument("--set-secret", metavar="SECRET",
                    help="写入飞书「签名校验」密钥并立即自检")
    ap.add_argument("--set-keyword", metavar="WORD",
                    help="写入飞书「自定义关键词」并立即自检")
    a = ap.parse_args(argv)

    patch: dict = {}
    if a.set_url:
        patch["url"] = a.set_url.strip()
        patch["enabled"] = True
    if a.set_secret:
        patch["secret"] = a.set_secret.strip()
    if a.set_keyword:
        patch["keyword"] = a.set_keyword.strip()
    if patch:
        print(f"已写入 {write_config(patch)}")
        # Configure and verify in one step: "配好了吗" must not be left to guesswork.
        a.test = a.show = True

    n = Notifier.from_config()
    if a.show or not a.test:
        print(json.dumps(n.describe(), ensure_ascii=False, indent=1))
        if not a.test:
            print(f"\n配置文件：{CONFIG_PATH}")
            print("示例：config/notify.example.json")
            return 0
    if not n._ready():
        print("通知未启用或配置不完整，无法发送测试消息。", file=sys.stderr)
        return 2
    r = n.send("✅ 测试消息 · 调仓通知",
               "如果你看到这条，说明调仓通知已经接通了。\n"
               f"provider = {n.provider}\n时间 = {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
