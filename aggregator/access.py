"""Optional access token for LAN mode (phone app / other devices on your Wi-Fi).

* By default the UI binds to 127.0.0.1, so only this computer can reach it and no
  token is needed.
* When it binds to a LAN address (``serve --lan`` / ``--host 0.0.0.0`` /
  ``server.host: 0.0.0.0``) every request that does NOT come from this computer
  must carry the token, either as a cookie (``agg_token``), an ``X-Access-Token``
  header, ``Authorization: Bearer <token>`` or ``?token=<token>`` once (that sets
  the cookie). Requests from 127.0.0.1 / ::1 are always allowed.

Where the token comes from (first match wins):
  1. env ``AGGREGATOR_TOKEN``
  2. ``server.access_token`` in config.local.yaml (gitignored)
  3. ``data/access_token.txt`` (gitignored; created automatically the first time
     you start LAN mode without 1 or 2)
``serve --lan --no-token`` (or env ``AGGREGATOR_NO_TOKEN=1``) disables it; only do
that on a network you fully trust.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket

from .config import ROOT, resolve

COOKIE = "agg_token"
TOKEN_FILE = "data/access_token.txt"
# no 0/o/1/l/i: easy to read off a screen and type on a phone
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def token_path():
    return resolve(TOKEN_FILE)


def disabled() -> bool:
    return os.environ.get("AGGREGATOR_NO_TOKEN", "").strip().lower() in ("1", "true", "yes")


def resolve_token(cfg: dict) -> str | None:
    """The configured token, or None (no token required)."""
    if disabled():
        return None
    env = os.environ.get("AGGREGATOR_TOKEN", "").strip()
    if env:
        return env
    conf = str(((cfg.get("server") or {}).get("access_token")) or "").strip()
    if conf:
        return conf
    p = token_path()
    if p.exists():
        t = p.read_text(encoding="utf-8").strip()
        return t or None
    return None


def new_token() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))  # ~79 bits
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def create_token_file(force: bool = False) -> str:
    p = token_path()
    if p.exists() and not force:
        t = p.read_text(encoding="utf-8").strip()
        if t:
            return t
    p.parent.mkdir(parents=True, exist_ok=True)
    t = new_token()
    p.write_text(t + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return t


def token_source(cfg: dict) -> str:
    if disabled():
        return "disabled (AGGREGATOR_NO_TOKEN)"
    if os.environ.get("AGGREGATOR_TOKEN", "").strip():
        return "env AGGREGATOR_TOKEN"
    if str(((cfg.get("server") or {}).get("access_token")) or "").strip():
        return "server.access_token in " + os.path.basename(cfg.get("_local_path") or cfg.get("_path") or "config")
    if token_path().exists():
        return str(token_path().relative_to(ROOT))
    return "none"


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    h = host.strip("[]").lower()
    if h == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def lan_ips() -> list[str]:
    """Best-effort list of this computer's LAN IPv4 addresses (primary first)."""
    ips: list[str] = []
    try:  # the address the OS would use to reach the internet (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not a.is_loopback and not a.is_link_local and ip not in out:
            out.append(ip)
    return out


def check(token: str | None, supplied: list[str | None]) -> bool:
    if not token:
        return True
    want = token.encode("utf-8")
    return any(s and hmac.compare_digest(s.strip().encode("utf-8"), want) for s in supplied)
