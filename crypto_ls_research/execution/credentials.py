"""API credential store for the trading desk -- demo and live in separate slots.

Rules this module exists to enforce
-----------------------------------
1. **Demo and live keys are not interchangeable.**  OKX issues demo keys from
   the demo-trading console; a live key presented with
   `x-simulated-trading: 1` fails with `50111 Invalid OK-ACCESS-KEY`, which
   looks exactly like a typo'd key.  Keeping the two in named slots means the
   UI can never accidentally send the wrong one, and the error message can say
   "this looks like a live key in the demo slot" instead of "invalid key".
2. **Secrets are write-only from the caller's point of view.**  `status()`
   returns a masked hint and a boolean -- never anything from which the secret
   could be reconstructed.  Nothing in this module is ever logged or echoed.
3. **Precedence: environment > file.**  A real deployment should keep secrets in
   the environment (nothing sensitive on disk); the JSON file is a convenience
   for the local console, written with restrictive permissions where the OS
   supports it and always via an atomic replace.
4. `paper` mode deliberately has no slot -- it must be impossible to give the
   local simulator a key and then wonder why orders show up at the exchange.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".."))
CONFIG_DIR = os.path.join(ROOT, "config")
CREDS_FILE = os.path.join(CONFIG_DIR, "okx_creds.json")
LOCK_FILE = os.path.join(CONFIG_DIR, "live.lock")

#: Modes that talk to OKX.  `paper` is absent on purpose.
EXCHANGE_MODES = ("demo", "live")

_ENV = {
    # demo keys are created inside OKX "Demo Trading -> Personal Center -> Demo Trading API"
    "demo": ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE"),
    "live": ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"),
}

_LOCK = threading.RLock()


@dataclass
class Creds:
    """One API key triple.  `source` is 'env' | 'file', for diagnostics only."""

    api_key: str
    secret_key: str
    passphrase: str
    source: str = "file"

    def __repr__(self) -> str:                                    # pragma: no cover
        # Never let a traceback or a log line print the secret.
        return f"Creds(api_key={mask(self.api_key)}, source={self.source})"

    __str__ = __repr__


def mask(v: Optional[str], head: int = 4, tail: int = 4) -> str:
    """Mask a secret for display.  Short values collapse to dots, not a prefix."""
    if not v:
        return ""
    if len(v) <= head + tail + 3:
        return "•" * 8
    return f"{v[:head]}…{v[-tail:]}"


def _read_file() -> Dict[str, dict]:
    try:
        with open(CREDS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_file(d: Dict[str, dict]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CREDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    try:                                            # best effort; Windows may ignore
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, CREDS_FILE)


def _from_env(mode: str) -> Optional[Creds]:
    names = _ENV.get(mode)
    if not names:
        return None
    k, s, p = (os.environ.get(n) for n in names)
    if k and s and p:
        return Creds(k, s, p, source="env")
    return None


def load_creds(mode: str) -> Optional[Creds]:
    """Env first, then the local file.  Returns None when nothing is configured."""
    if mode not in EXCHANGE_MODES:
        return None
    c = _from_env(mode)
    if c:
        return c
    with _LOCK:
        slot = _read_file().get(mode) or {}
    k, s, p = slot.get("api_key"), slot.get("secret_key"), slot.get("passphrase")
    if k and s and p:
        return Creds(k, s, p, source="file")
    return None


def save_creds(mode: str, api_key: str, secret_key: str, passphrase: str) -> None:
    if mode not in EXCHANGE_MODES:
        raise ValueError(f"cannot store credentials for mode {mode!r}")
    if not (api_key and secret_key and passphrase):
        raise ValueError("api_key / secret_key / passphrase must all be non-empty")
    with _LOCK:
        d = _read_file()
        d[mode] = {"api_key": api_key.strip(), "secret_key": secret_key.strip(),
                   "passphrase": passphrase.strip()}
        _write_file(d)


def delete_creds(mode: str) -> None:
    if mode not in EXCHANGE_MODES:
        raise ValueError(f"unknown mode {mode!r}")
    with _LOCK:
        d = _read_file()
        if mode in d:
            del d[mode]
            _write_file(d)


def creds_status() -> Dict[str, dict]:
    """Masked, safe-to-serialise status for every mode the UI can select."""
    out: Dict[str, dict] = {}
    for mode in ("paper",) + EXCHANGE_MODES:
        c = load_creds(mode)
        env_configured = _from_env(mode) is not None if mode in EXCHANGE_MODES else False
        out[mode] = {
            "mode": mode,
            "configured": c is not None,
            "key_hint": mask(c.api_key) if c else "",
            "source": c.source if c else None,
            # `env` wins over the file, so a stale file entry is worth flagging:
            # editing it in the UI would appear to "not take".
            "env_override": bool(env_configured),
            "env_vars": list(_ENV.get(mode, ())),
        }
    return out


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------
def kill_switch_path() -> str:
    return LOCK_FILE


def kill_switch_on() -> bool:
    """A plain file is the one mechanism that survives a crashed UI, a stuck
    browser tab and a runaway child process.  Touch it and nothing trades."""
    return os.path.exists(LOCK_FILE)


def set_kill_switch(on: bool) -> bool:
    """Arm or disarm the kill switch; return the state that is now in effect.

    Disarming is *best effort and then verified*, never assumed:

    * Removing one file can be refused by a host-level guard, and this machine's
      guard aborts with `SystemExit` -- a `BaseException`, so an `except OSError`
      lets it escape and the disarm request dies mid-flight.
    * Swallowing that silently would be worse than failing: the caller would
      report "熔断已解除" while the lock file is still there.

    So we re-read the file and report the truth.  The failure direction is safe
    either way -- a surviving lock file means `kill_switch_on()` stays True and
    every order is still blocked (fail closed).
    """
    if on:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write("trading halted\n")
    else:
        try:
            os.remove(LOCK_FILE)
        except BaseException:                                    # noqa: BLE001
            pass
    return kill_switch_on()
