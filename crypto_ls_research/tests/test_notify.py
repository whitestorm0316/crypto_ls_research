"""Tests for the rebalance notifier.

The load-bearing properties, in order of how much damage their absence causes:

1. **A notification failure must never reach the order path.**  A dead webhook is
   an annoyance; an exception thrown mid-rebalance is a half-sent book.  So
   `send()` never raises and `AutoTrader._notify()` swallows even a broken
   formatter.
2. **The credential must not leak into the log.**  These URLs end up in a file
   people read and paste -- and the credential is *not* always a query parameter
   (飞书 and Server酱 put it in the path), so both places are covered.
3. **Routine ticks must be silent.**  "Not a rebalance day" happens ~24x/day;
   pushing it is how a person learns to ignore the channel.
4. **A repeating failure must not become 24 messages.**
"""
from __future__ import annotations

import json
import os
import stat
import time

import pytest

from crypto_ls_research.execution import auto_trader as at_mod
from crypto_ls_research.execution import notify as notify_mod
from crypto_ls_research.execution import store as store_mod
from crypto_ls_research.execution.auto_trader import AutoTrader
from crypto_ls_research.execution.notify import (Notifier, format_rebalance,
                                                 notify_for, redact)
from crypto_ls_research.tests.test_auto_trader import FakeEngine


@pytest.fixture
def tmp_live(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "LIVE_DIR", str(tmp_path / "live"))
    return tmp_path / "live"


class Recorder:
    """Stands in for the HTTP call so nothing in this file touches the network.

    The default body is empty on purpose: a stub transport says nothing about
    the service's verdict, and defaulting to a *wecom* success body would make
    every other provider's assertion depend on the "field absent -> ok"
    fallback without anyone noticing.  Each provider's real success body is
    asserted explicitly in the success tests.
    """

    def __init__(self, status=200, body="{}"):
        self.calls: list = []
        self.status, self.body = status, body

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        return self.status, self.body


WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPER-SECRET-KEY"


def _n(rec, **cfg):
    base = {"enabled": True, "provider": "wecom", "url": WECOM}
    base.update(cfg)
    return Notifier(base, mode="demo", post=rec)


# ---------------------------------------------------------------------------
# never let a push break trading
# ---------------------------------------------------------------------------
def test_send_never_raises_when_the_transport_explodes():
    def boom(url, payload, timeout):
        raise OSError("no route to host")

    r = _n(boom).send("t", "b")
    assert r["ok"] is False and "no route to host" in r["error"]


def test_send_never_raises_on_an_http_error_status():
    r = _n(Recorder(status=500, body="oops")).send("t", "b")
    assert r["ok"] is False and r["status"] == 500


def test_a_broken_formatter_cannot_kill_a_rebalance(tmp_live, monkeypatch):
    """Belt and braces: `send()` swallows everything, but the *formatter* runs
    outside it, and a rebalance must survive that too."""
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    monkeypatch.setattr(at_mod, "notify_for",
                        lambda out, n: (_ for _ in ()).throw(RuntimeError("bad fmt")))
    bot = AutoTrader(engine=FakeEngine(due=True), interval_min=60,
                     notifier=_n(Recorder()))
    out = bot.once()
    assert out["action"] == "traded"                    # the trade still happened
    assert out["notify"]["ok"] is False and "bad fmt" in out["notify"]["error"]


def test_a_failed_push_is_visible_in_the_audit_and_the_heartbeat(tmp_live, monkeypatch):
    """「告诉我了吗」must be answerable from the artefact, not from memory."""

    def boom(url, payload, timeout):
        raise OSError("boom")

    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    bot = AutoTrader(engine=FakeEngine(due=True), interval_min=60,
                     notifier=_n(boom))
    bot.once()
    st = json.loads(open(bot.state_path(), encoding="utf-8").read())
    assert st["last"]["notify"]["ok"] is False
    assert "boom" in st["last"]["notify"]["error"]


def test_the_daemon_picks_up_a_config_written_after_it_started(tmp_live, monkeypatch):
    """The daemon runs for weeks; configuring the webhook afterwards must not
    require a restart.

    If it did, `--test` (a fresh process) would pass while the running daemon
    stayed silent -- a "it works but it doesn't" state that reads as "the
    strategy did nothing".
    """
    cfg = tmp_live.parent / "notify.json"
    monkeypatch.setattr(notify_mod, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    rec = Recorder(status=200, body='{"code":0}')
    monkeypatch.setattr(notify_mod, "_post_json", rec)

    bot = AutoTrader(engine=FakeEngine(due=True), interval_min=60)
    assert bot.notifier.describe()["ready"] is False
    bot.once()
    assert rec.calls == []                       # 没配 → 一个字节都不发

    notify_mod.write_config({"url": FEISHU, "enabled": True}, path=str(cfg))
    bot.once()                                   # 同一个实例，没重启
    assert len(rec.calls) == 1 and rec.calls[0]["url"] == FEISHU
    assert bot.last["notify"]["ok"] is True


def test_an_injected_notifier_is_never_swapped_for_the_file(tmp_live, monkeypatch,
                                                            tmp_path):
    """Otherwise every test in this file would quietly start reading the
    developer's own config/notify.json.

    The file is touched *and* given a different provider first: without that,
    the mtime never changes, the reload never fires, and the assertion passes
    even when the guard is deleted.
    """
    cfg = tmp_path / "notify.json"
    cfg.write_text(json.dumps({"enabled": True, "provider": "feishu",
                               "url": FEISHU}), encoding="utf-8")
    monkeypatch.setattr(notify_mod, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    injected = _n(Recorder())                       # 这个桩是 wecom
    bot = AutoTrader(engine=FakeEngine(due=True), interval_min=60, notifier=injected)
    later = time.time() + 10
    os.utime(str(cfg), (later, later))
    assert bot._notifier_now() is injected
    assert bot._notifier_now().provider == "wecom"


# ---------------------------------------------------------------------------
# the credential
# ---------------------------------------------------------------------------
def test_the_webhook_key_is_redacted_everywhere():
    """The key lives in the query string and these strings land in a log file."""
    assert redact(WECOM) == "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?***"
    assert "SUPER-SECRET-KEY" not in redact(WECOM)

    def boom(url, payload, timeout):
        raise OSError("connection refused")

    r = _n(boom).send("t", "b")
    assert "SUPER-SECRET-KEY" not in json.dumps(r, ensure_ascii=False)
    assert "SUPER-SECRET-KEY" not in json.dumps(_n(Recorder()).describe(),
                                                ensure_ascii=False)


def test_redact_leaves_a_keyless_url_alone():
    assert redact("https://example.com/hook") == "https://example.com/hook"
    # the path *prefix* stays readable; only the token tail goes
    assert (redact("https://open.feishu.cn/open-apis/bot/v2/hook")
            == "https://open.feishu.cn/open-apis/bot/v2/hook")
    assert redact("") == ""


def test_a_key_in_the_path_is_redacted_too():
    """飞书 hides the token in the **path**, not in the query.

    Masking only `?key=` wrote the token in plaintext into `logs/auto_demo.log`
    and into the world-readable heartbeat `artifacts/live/*/auto.json` -- found
    by grepping the real log after configuring a real bot.
    """
    assert redact(FEISHU) == "https://open.feishu.cn/open-apis/bot/v2/hook/***"
    assert "11111111-2222-3333" not in redact(FEISHU)
    # Server酱 keeps its key in the path as well.
    assert "SCT9999" not in redact("https://sctapi.ftqq.com/SCT9999abcdef.send")


def test_a_feishu_token_never_reaches_the_log_or_the_heartbeat(tmp_live, monkeypatch):
    """End to end: everything the notifier *writes* must be token-free.

    The transport still gets the real URL -- redaction is display-only, and the
    send tests above pin that.
    """
    token = "11111111-2222-3333"

    def boom(url, payload, timeout):
        raise OSError("connection refused")

    n = _n(boom, provider="feishu", url=FEISHU)
    assert token not in json.dumps(n.send("t", "b"), ensure_ascii=False)
    assert token not in json.dumps(n.describe(), ensure_ascii=False)

    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    bot = AutoTrader(engine=FakeEngine(due=True), interval_min=60, notifier=n)
    bot.once()
    assert token not in open(bot.state_path(), encoding="utf-8").read()
    assert token not in open(bot.log_path(), encoding="utf-8").read()


# ---------------------------------------------------------------------------
# policy: what is worth a push
# ---------------------------------------------------------------------------
def test_routine_ticks_are_silent():
    """`not_due` is the normal case ~24 times a day.  Pushing it trains the
    person to ignore the channel, which is worse than no channel."""
    n = _n(Recorder())
    assert n.enabled_for("traded") is True
    assert n.enabled_for("blocked") is True
    assert n.enabled_for("error") is True
    assert n.enabled_for("skip") is False
    assert n.enabled_for("noop") is False


def test_notify_on_is_configurable():
    n = _n(Recorder(), notify_on=["traded"])
    assert n.enabled_for("traded") is True and n.enabled_for("blocked") is False


def test_modes_are_respected():
    n = _n(Recorder(), modes=["live"])
    assert n.enabled_for("traded", "live") is True
    assert n.enabled_for("traded", "demo") is False


def test_a_repeating_failure_is_suppressed_but_a_trade_never_is():
    """A webhook that broke an hour ago must not produce 24 pushes; but every
    completed rebalance matters, so `traded` bypasses the window entirely."""
    n = _n(Recorder(), dedup_sec=3600)
    assert n.send("t", "b", key="demo|error|X", action="error")["ok"] is True
    second = n.send("t", "b", key="demo|error|X", action="error")
    assert second["ok"] is False and second["skipped"] == "重复通知已抑制"
    # a different key is not suppressed
    assert n.send("t", "b", key="demo|error|Y", action="error")["ok"] is True
    # traded always goes
    assert n.send("t", "b", key="k", action="traded")["ok"] is True
    assert n.send("t", "b", key="k", action="traded")["ok"] is True


def test_disabled_or_unconfigured_is_a_clean_no_op():
    assert _n(Recorder(), enabled=False).send("t", "b")["ok"] is False
    assert Notifier({"provider": "none"}).describe()["ready"] is False
    # pushplus needs a token, not a url
    assert Notifier({"enabled": True, "provider": "pushplus"}).describe()["ready"] is False
    assert Notifier({"enabled": True, "provider": "pushplus",
                     "token": "tok"}).describe()["ready"] is True


def test_a_missing_config_file_means_off_not_a_crash(tmp_path):
    n = Notifier.from_config(path=str(tmp_path / "nope.json"))
    assert n.enabled is False and n.describe()["ready"] is False


def test_a_corrupt_config_file_means_off_not_a_crash(tmp_path):
    p = tmp_path / "notify.json"
    p.write_text("{ half-written", encoding="utf-8")
    assert Notifier.from_config(path=str(p)).enabled is False


def test_an_unknown_provider_is_rejected_loudly():
    """A typo'd provider must not silently become "no notifications"."""
    with pytest.raises(ValueError):
        Notifier({"provider": "weechat"})


# ---------------------------------------------------------------------------
# the payload, per provider
# ---------------------------------------------------------------------------
def test_wecom_sends_markdown_to_the_configured_url():
    rec = Recorder()
    r = _n(rec).send("标题", "正文")
    assert r["ok"] is True and len(rec.calls) == 1
    p = rec.calls[0]["payload"]
    assert p["msgtype"] == "markdown"
    assert p["markdown"]["content"].startswith("**标题**") and "正文" in p["markdown"]["content"]


def test_serverchan_posts_a_form_with_title_and_desp():
    """Form posts are injectable too.

    The first version of this test passed the *json* recorder and asserted
    `rec.calls == []`, which meant it silently performed a real HTTP request to
    Server酱 instead of observing anything.  A test that cannot see the payload
    is not testing the payload.
    """
    rec = Recorder()
    n = Notifier({"enabled": True, "provider": "serverchan",
                  "url": "https://sctapi.ftqq.com/KEY.send"}, post_form=rec)
    r = n.send("标题", "正文")
    assert r["ok"] is True and len(rec.calls) == 1
    assert rec.calls[0]["url"] == "https://sctapi.ftqq.com/KEY.send"
    assert rec.calls[0]["payload"] == {"title": "标题", "desp": "正文"}


def test_pushplus_uses_its_fixed_endpoint_and_the_token():
    rec = Recorder()
    n = Notifier({"enabled": True, "provider": "pushplus", "token": "TOK"}, post=rec)
    r = n.send("标题", "正文")
    assert r["ok"] is True
    assert rec.calls[0]["url"] == "https://www.pushplus.plus/send"
    assert rec.calls[0]["payload"]["token"] == "TOK"
    assert rec.calls[0]["payload"]["template"] == "markdown"


def test_generic_webhook_posts_the_title_and_text():
    rec = Recorder()
    n = Notifier({"enabled": True, "provider": "webhook",
                  "url": "https://example.com/hook"}, mode="live", post=rec)
    n.send("标题", "正文")
    assert rec.calls[0]["payload"] == {"title": "标题", "text": "正文", "mode": "live"}


# ---------------------------------------------------------------------------
# 飞书自定义机器人
# ---------------------------------------------------------------------------
FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/11111111-2222-3333"


def _feishu(rec, **cfg):
    base = {"enabled": True, "provider": "feishu", "url": FEISHU}
    base.update(cfg)
    return Notifier(base, mode="demo", post=rec)


def test_feishu_posts_a_post_message_with_a_title_and_one_paragraph_per_line():
    """`post`, not `text`: the itemised 「被拒 N 笔」 list has to stay a list."""
    rec = Recorder(body='{"code":0,"msg":"success"}')
    r = _feishu(rec).send("✅ 调仓完成 · 模拟盘", "第一行\n第二行")
    assert r["ok"] is True
    p = rec.calls[0]["payload"]
    assert p["msg_type"] == "post"
    assert p["content"]["post"]["zh_cn"]["title"] == "✅ 调仓完成 · 模拟盘"
    paras = p["content"]["post"]["zh_cn"]["content"]
    assert paras == [[{"tag": "text", "text": "第一行"}],
                     [{"tag": "text", "text": "第二行"}]]


def test_feishu_omits_the_signature_unless_a_secret_is_configured():
    """A bot using 关键词 or IP 白名单 security has no secret to sign with, and
    sending a bogus signature would be rejected with 19021."""
    rec = Recorder(body='{"code":0}')
    _feishu(rec).send("t", "b")
    assert "sign" not in rec.calls[0]["payload"]
    assert "timestamp" not in rec.calls[0]["payload"]


def test_feishu_signature_matches_the_documented_algorithm():
    """飞书's HMAC is the unusual one: the *key* is `"{ts}\\n{secret}"` and the
    signed message is empty.  The conventional `hmac(key=secret, msg=body)`
    fails with `code 19021`, so this pins the odd shape against a future
    "cleanup"."""
    import base64
    import hashlib
    import hmac as hmac_mod

    secret = "SIGN-KEY-abc"
    rec = Recorder(body='{"code":0}')
    _feishu(rec, secret=secret).send("t", "b")
    p = rec.calls[0]["payload"]
    assert set(p) >= {"timestamp", "sign"}
    ts = p["timestamp"]
    assert ts.isdigit() and abs(int(ts) - int(time.time())) < 60
    expected = base64.b64encode(hmac_mod.new(
        f"{ts}\n{secret}".encode("utf-8"), b"",
        digestmod=hashlib.sha256).digest()).decode("ascii")
    assert p["sign"] == expected
    conventional = base64.b64encode(hmac_mod.new(
        secret.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()).decode("ascii")
    assert p["sign"] != conventional


def test_feishu_prepends_the_security_keyword_but_only_when_configured():
    """飞书 rejects a message lacking the configured keyword with `code 19024`.
    Prepending is more robust than hoping the title happens to contain it."""
    rec = Recorder(body='{"code":0}')
    _feishu(rec, keyword="调仓").send("✅ 调仓完成", "正文")
    assert rec.calls[0]["payload"]["content"]["post"]["zh_cn"]["title"] == "调仓 ✅ 调仓完成"
    rec2 = Recorder(body='{"code":0}')
    _feishu(rec2).send("标题", "正文")
    assert rec2.calls[0]["payload"]["content"]["post"]["zh_cn"]["title"] == "标题"


def test_the_feishu_secret_is_never_printed():
    n = _feishu(Recorder(), secret="SIGN-KEY-abc")
    assert n.describe()["secret_set"] is True
    assert "SIGN-KEY-abc" not in json.dumps(n.describe(), ensure_ascii=False)


def test_feishu_strips_markdown_that_post_cannot_render():
    """飞书's `post` is not markdown, so `**被拒 2 笔**` would show the asterisks.

    A 飞书 card renders markdown and looks better, but the 自定义关键词 security
    filter is only documented for text/title params -- so a card risks a silent
    `code 19024` where a post does not.  The emphasis is what gives way.
    """
    rec = Recorder(body='{"code":0}')
    _feishu(rec).send("标题", "**被拒 2 笔**\n· X-USDT-SWAP `51001` 下线")
    paras = rec.calls[0]["payload"]["content"]["post"]["zh_cn"]["content"]
    lines = [p[0]["text"] for p in paras]
    assert lines == ["被拒 2 笔", "· X-USDT-SWAP 51001 下线"]
    assert "**" not in "".join(lines) and "`" not in "".join(lines)


def test_the_real_trade_message_carries_no_leftover_markdown_to_feishu():
    """The formatter's output is written for 企业微信's markdown mode; this is the
    end-to-end check that nothing leaks through to a provider that cannot render
    it."""
    title, text = format_rebalance(TRADE)
    assert "**" in text, "fixture 本身得带 markdown，否则这条断言是恒真的"
    rec = Recorder(body='{"code":0}')
    _feishu(rec).send(title, text)
    blob = json.dumps(rec.calls[0]["payload"], ensure_ascii=False)
    assert "**" not in blob and "`" not in blob
    assert f"被拒 {len(TRADE['failures'])} 笔" in blob


# ---------------------------------------------------------------------------
# HTTP 200 is not a verdict -- the body is
# ---------------------------------------------------------------------------
REJECTIONS = [
    ("feishu", {"enabled": True, "provider": "feishu", "url": FEISHU},
     '{"code":19021,"msg":"sign match fail or timestamp is not within one hour"}',
     "19021", "sign match fail"),
    ("wecom", {"enabled": True, "provider": "wecom", "url": WECOM},
     '{"errcode":93000,"errmsg":"invalid webhook url"}',
     "93000", "invalid webhook url"),
    ("serverchan", {"enabled": True, "provider": "serverchan",
                    "url": "https://sctapi.ftqq.com/KEY.send"},
     '{"code":40001,"message":"bad pushtype"}', "40001", "bad pushtype"),
    ("pushplus", {"enabled": True, "provider": "pushplus", "token": "TOK"},
     '{"code":999,"msg":"token 无效"}', "999", "token 无效"),
]


@pytest.mark.parametrize("name,cfg,body,needle,human", REJECTIONS,
                         ids=[r[0] for r in REJECTIONS])
def test_a_rejected_push_returned_with_http_200_is_reported_as_a_failure(
        name, cfg, body, needle, human):
    """Every one of these services answers a *rejected* push with HTTP 200.

    A status-only check calls that `ok: True`, so a rotated webhook or a failed
    signature reads as "the strategy did nothing" instead of "your
    notifications are broken" -- which is the one failure a person cannot
    notice by themselves.

    The service's own wording is carried through too: `code=19021` alone does
    not tell you it was the signature, and that is the difference between a
    two-minute fix and an afternoon.
    """
    rec = Recorder(status=200, body=body)
    r = Notifier(cfg, mode="demo", post=rec, post_form=rec).send("t", "b")
    assert r["ok"] is False, f"{name} 把 HTTP 200 当成了成功"
    assert needle in r["error"], r
    assert human in r["error"], r


@pytest.mark.parametrize("name,cfg,body", [
    ("feishu", {"enabled": True, "provider": "feishu", "url": FEISHU},
     '{"code":0,"msg":"success"}'),
    ("wecom", {"enabled": True, "provider": "wecom", "url": WECOM},
     '{"errcode":0,"errmsg":"ok"}'),
    ("serverchan", {"enabled": True, "provider": "serverchan",
                    "url": "https://sctapi.ftqq.com/KEY.send"}, '{"code":0}'),
    ("pushplus", {"enabled": True, "provider": "pushplus", "token": "TOK"},
     '{"code":200,"msg":"请求成功"}'),
], ids=["feishu", "wecom", "serverchan", "pushplus"])
def test_an_accepted_push_is_still_reported_as_success(name, cfg, body):
    """The other half of the pair: a body check that fails everything is just a
    different way of being useless."""
    rec = Recorder(status=200, body=body)
    assert Notifier(cfg, mode="demo", post=rec, post_form=rec).send("t", "b")["ok"] is True


def test_a_body_that_cannot_be_read_does_not_invent_a_failure():
    """A non-JSON 200, or a JSON body with no verdict field, must not be turned
    into a false alarm -- only an explicit rejection counts."""
    for body in ("", "OK", "<html>hello</html>", '{"result":"whatever"}', '["a"]'):
        rec = Recorder(status=200, body=body)
        assert _n(rec).send("t", "b")["ok"] is True, body


def test_the_generic_webhook_is_exempt_from_the_body_verdict():
    """The peer defines its own body; `{"code": 1}` there may well mean success,
    so reading a verdict out of it would be guessing."""
    rec = Recorder(status=200, body='{"code":1,"msg":"not our vocabulary"}')
    n = Notifier({"enabled": True, "provider": "webhook",
                  "url": "https://example.com/hook"}, post=rec)
    assert n.send("t", "b")["ok"] is True


def test_a_200_with_a_rejection_in_the_body_reaches_the_heartbeat(tmp_live, monkeypatch):
    """「告诉我了吗」must be answerable from the artefact -- including the case
    where the transport succeeded and the service refused."""
    monkeypatch.setattr(at_mod, "kill_switch_on", lambda: False)
    rec = Recorder(status=200, body='{"errcode":93000,"errmsg":"invalid webhook url"}')
    bot = AutoTrader(engine=FakeEngine(due=True), interval_min=60, notifier=_n(rec))
    bot.once()
    st = json.loads(open(bot.state_path(), encoding="utf-8").read())
    assert st["last"]["action"] == "traded"
    assert st["last"]["notify"]["ok"] is False
    assert "93000" in st["last"]["notify"]["error"]


def test_an_over_long_body_is_truncated_not_rejected():
    """企业微信 caps the body; a long failure list must degrade, not 400.

    The contract is on the **whole** payload including the title -- slicing the
    body to the full budget and then prepending a title is how a "truncated"
    message still comes back over the limit.
    """
    rec = Recorder()
    n = _n(rec, max_bytes=200)
    n.send("标题也要算进去", "あ" * 500)
    body = rec.calls[0]["payload"]["markdown"]["content"]
    assert len(body.encode("utf-8")) <= 200
    assert "截断" in body and "标题也要算进去" in body


# ---------------------------------------------------------------------------
# the CLI: configuring must be one step, and it must verify itself
# ---------------------------------------------------------------------------
def test_write_config_seeds_from_the_template_and_keeps_the_notes(tmp_path):
    """A person pasting a URL should get the explanatory keys for free."""
    p = tmp_path / "notify.json"
    notify_mod.write_config({"url": "https://example.com/hook"}, path=str(p))
    cfg = json.loads(p.read_text(encoding="utf-8"))
    assert cfg["url"] == "https://example.com/hook"
    assert cfg["provider"] == "feishu"                 # 来自模板
    assert any(k.startswith("_") for k in cfg)         # 说明键保留
    assert not (tmp_path / "notify.json.tmp").exists()  # 没留半截文件


def test_write_config_merges_instead_of_clobbering(tmp_path):
    p = tmp_path / "notify.json"
    p.write_text(json.dumps({"enabled": True, "url": "old", "dedup_sec": 60}),
                 encoding="utf-8")
    notify_mod.write_config({"url": "new"}, path=str(p))
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "enabled": True, "url": "new", "dedup_sec": 60}


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX 权限位在 Windows 上不可表达（chmod 无法产生 0600，"
                           "且 stat 一律读回 0666）；此处真正要守的是 POSIX 侧的行为")
def test_write_config_is_private_because_the_url_carries_a_key(tmp_path):
    """The webhook URL carries a key, so the config must not be world-readable.

    Skipped on Windows rather than asserted: NTFS has no POSIX mode, `os.chmod`
    there only toggles the read-only bit, and `stat.S_IMODE` reads back `0o666`
    regardless -- so an assertion here would fail on every Windows checkout
    while telling us nothing about whether the file is actually protected.
    The production path already tolerates this (`chmod` is wrapped in
    `except OSError`), which is the behaviour that matters.
    """
    p = tmp_path / "notify.json"
    notify_mod.write_config({"url": "https://example.com/hook?key=SEC"}, path=str(p))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_old_config_intact(tmp_path):
    """Written via temp + rename, so an interrupted write cannot leave a
    truncated config behind -- which would read as "notifications are off"."""
    p = tmp_path / "notify.json"
    p.write_text(json.dumps({"url": "old"}), encoding="utf-8")
    with pytest.raises(TypeError):
        notify_mod.write_config({"url": object()}, path=str(p))
    assert json.loads(p.read_text(encoding="utf-8")) == {"url": "old"}


def test_set_url_enables_it_and_verifies_in_the_same_step(tmp_path, monkeypatch,
                                                          capsys):
    """`--set-url` must also *send*: leaving "did that work?" to a second command
    is how a broken webhook survives until the first real rebalance."""
    monkeypatch.setattr(notify_mod, "CONFIG_PATH", str(tmp_path / "notify.json"))
    rec = Recorder(status=200, body='{"code":0}')
    monkeypatch.setattr(notify_mod, "_post_json", rec)
    assert notify_mod.main(["--set-url", FEISHU]) == 0
    assert len(rec.calls) == 1 and rec.calls[0]["url"] == FEISHU
    cfg = json.loads((tmp_path / "notify.json").read_text(encoding="utf-8"))
    assert cfg["enabled"] is True and cfg["url"] == FEISHU
    assert "已写入" in capsys.readouterr().out


def test_the_cli_fails_loudly_when_the_service_refuses(tmp_path, monkeypatch):
    """Exit 1, not 0 -- the whole point of reading the body."""
    monkeypatch.setattr(notify_mod, "CONFIG_PATH", str(tmp_path / "notify.json"))
    monkeypatch.setattr(notify_mod, "_post_json",
                        Recorder(status=200,
                                 body='{"code":19021,"msg":"sign match fail"}'))
    assert notify_mod.main(["--set-url", FEISHU]) == 1


def test_the_cli_says_so_when_nothing_is_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(notify_mod, "CONFIG_PATH", str(tmp_path / "notify.json"))
    assert notify_mod.main(["--test"]) == 2


def test_set_secret_and_keyword_are_written_and_the_secret_is_not_printed(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(notify_mod, "CONFIG_PATH", str(tmp_path / "notify.json"))
    rec = Recorder(status=200, body='{"code":0}')
    monkeypatch.setattr(notify_mod, "_post_json", rec)
    rc = notify_mod.main(["--set-url", FEISHU, "--set-secret", "SIGN-KEY-abc",
                          "--set-keyword", "调仓"])
    assert rc == 0
    cfg = json.loads((tmp_path / "notify.json").read_text(encoding="utf-8"))
    assert cfg["secret"] == "SIGN-KEY-abc" and cfg["keyword"] == "调仓"
    assert "SIGN-KEY-abc" not in capsys.readouterr().out   # --show 只打印布尔
    p = rec.calls[0]["payload"]
    assert p["sign"] and p["timestamp"]
    assert p["content"]["post"]["zh_cn"]["title"].startswith("调仓")


# ---------------------------------------------------------------------------
# the message itself -- this is the feature
# ---------------------------------------------------------------------------
TRADE = {
    "mode": "demo", "action": "traded", "reason": "executed",
    "decision_ts": "2026-09-29T02:00:00+00:00",
    "next_decision_ts": "2026-10-02T02:00:00+00:00",
    "nav": 54010.02, "target_gross": 1.1088,
    "n_orders": 19, "n_ok": 11, "n_fail": 8,
    "gross_realised": 13750.30, "turnover_frac": 0.0635,
    "run_id": "demo_20260929_020000_ab12cd",
    "failures": [{"instId": "PUMP-USDT-SWAP", "code": "51087",
                  "error": "该币种已下架"}],
    "warnings": ["7 个目标腿在当前场所不存在，会被逐笔拒绝"],
}


def test_a_trade_message_carries_the_numbers_a_person_needs():
    title, text = format_rebalance(TRADE)
    assert "调仓完成" in title and "模拟盘" in title
    assert "19" in text and "成交 11" in text and "失败 8" in text
    assert "13,750" in text                      # 实际毛敞口
    assert "6.35%" in text                       # 换手
    assert "1.109×" in text                      # 目标毛敞口
    assert "54,010" in text                      # 净值
    # 信号日按**北京时间**渲染：`decision_ts` 是 UTC 02:00，也就是本地 10:00。
    # 回测网格锚在 UTC 02:00 = 北京 10:00，所以把 ISO 原样截断会在推送里写出
    # 一个差 8 小时的时间，而这是操作者不用打开控制台就会看到的那一面。
    assert "2026-09-29 10:00" in text            # 信号日（北京）
    assert "2026-10-02 10:00" in text            # 下次调仓（北京）
    assert "2026-09-29 02:00" not in text        # 不得回退成 UTC 直显
    assert "demo_20260929_020000_ab12cd" in text


def test_a_trade_message_names_why_orders_were_rejected():
    """A rejected order never enters the venue's order book, so the reason exists
    only in the execution response.  If it is not in the push, the user has to go
    digging -- which is exactly what they had to do before."""
    _, text = format_rebalance(TRADE)
    assert "被拒 1 笔" in text
    assert "PUMP-USDT-SWAP" in text and "51087" in text and "该币种已下架" in text


def test_a_blocked_message_says_nothing_was_sent():
    out = dict(TRADE, action="blocked", reason="risk_gate",
               blocked_by=["max_gross_notional"])
    title, text = format_rebalance(out)
    assert "风控拦截" in title
    assert "未发送任何订单" in text
    assert "max_gross_notional" in text


def test_an_error_message_carries_the_exception():
    out = {"mode": "demo", "action": "error", "reason": "AmbiguousError",
           "error": "响应丢失，需对账：timeout"}
    title, text = format_rebalance(out)
    assert "出错" in title and "响应丢失" in text


def test_a_skip_is_not_sent_even_if_the_formatter_is_asked_for_one():
    """`notify_for` is the single gate; a skip must not slip past it."""
    out = {"mode": "demo", "action": "skip", "reason": "not_due"}
    rec = Recorder()
    r = notify_for(out, _n(rec))
    assert r["ok"] is False and r["skipped"] == "该结果不推送" and rec.calls == []


def test_missing_fields_do_not_produce_the_string_none():
    """Half a payload must degrade to dashes, not to `None` in the message."""
    _, text = format_rebalance({"mode": "demo", "action": "traded"})
    assert "None" not in text and "nan" not in text


# ---------------------------------------------------------------------------
# 北京时间渲染
# ---------------------------------------------------------------------------
# 全部内部时间戳都是 UTC：面板 bar 索引是 UTC，回测网格锚在 UTC 02:00，推送里
# 的 `decision_ts` 也是 UTC。把 ISO 串直接截断，就等于在北京时间 10:00 做出的
# 决策上写「02:00」——而这恰好是操作者不打开控制台就会看到的那一行。

def test_display_ts_renders_utc_as_beijing():
    d = store_mod.display_ts
    assert d("2026-09-29T02:00:00+00:00") == "2026-09-29 10:00"   # 网格锚点
    assert d("2026-09-28T14:00:00+00:00") == "2026-09-28 22:00"
    assert d("2026-09-26T23:00:00Z") == "2026-09-27 07:00"        # 跨日
    assert d(1790560800) == "2026-09-28 10:00"                    # epoch 秒


def test_display_ts_does_not_depend_on_the_process_timezone(monkeypatch):
    """同一时刻在任何机器上必须是同一串 —— 这正是不能依赖本地时区的原因。

    直接用 `time.localtime` / 无 tzinfo 的 `strptime` 就会在 CI（通常 UTC）与
    本地（UTC+08:00）之间飘 8 小时，而两边都"看起来对"。
    """
    original = os.environ.get("TZ")

    def render():
        return store_mod.display_ts("2026-09-29T02:00:00+00:00")

    first = render()
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset() if hasattr(time, "tzset") else None
    second = render()
    if original is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", original)
    time.tzset() if hasattr(time, "tzset") else None
    assert first == second == "2026-09-29 10:00"


def test_display_ts_degrades_instead_of_raising():
    """渲染路径不能因为一个坏字段就抛出 —— 丢一条推送比显示原始串糟得多。"""
    d = store_mod.display_ts
    assert d("") == "" and d(None) == ""
    assert d("garbage") == "garbage"
    assert d("2026-09-29T02:00:00") == "2026-09-29 10:00"   # 无 tzinfo 视为 UTC


def test_a_naked_iso_string_is_still_converted_not_truncated():
    """最容易漏的形态：后端在某些路径上给出不带偏移的 ISO 串。

    按「无 tzinfo = UTC」解释，所以仍然要 +8 —— 如果实现退化成
    `s[:16].replace('T',' ')`，这一条会失败。
    """
    assert store_mod.display_ts("2026-09-29T02:00:00") == "2026-09-29 10:00"
