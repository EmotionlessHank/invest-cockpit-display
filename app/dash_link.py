"""
dash_link.py — Dashboard link "issue + authenticate" (pure stdlib, single source in app/,
deployed to skill scripts like strategy.py).

Why pure stdlib: skill scripts (show_progress / record_fill) run under the system Python managed
by the agent platform; itsdangerous only exists in the dashboard venv and is unlikely to be available in the
system Python — we cannot depend on it. HMAC-SHA256 + timestamp self-implementation is security-
equivalent and has zero dependencies.

Dual-factor authentication (24h code expiry design):
  · Static token (identity layer): HMAC(SECRET, "btc-dashboard-v1")[:24] — permanent, never changes.
  · Time-limited code (expiry layer): base64(timestamp + HMAC signature), expires in 24h.
    HMAC prevents forgery; timestamp defines expiry.
Either factor failing → app.py returns a clean fake 404 (does not reveal dashboard existence).

Device cookie (90-day device trust): clicking a TG link (URL dual-factor) sets a 90-day signed
cookie, after which the browser can access /d directly without a new link.
Format mirrors code, but HMAC uses domain-separation prefix _COOKIE_CTX — cookie and code are
mutually non-impersonatable (otherwise a 24h code would effectively become valid for 90 days).
Revocation = rotate SECRET → all devices invalidated instantly (stateless, zero server storage).

SECRET is read from env DASHBOARD_SECRET; fallback reads the dashboard .env (same-host 600 perms;
not written to SKILL/git to prevent leakage).

Honest threat model: rotation prevents "stale-credential replay" (old screenshots / forwarded old
links); it does NOT prevent "real-time credential theft" (attacker who has compromised TG/server
can forge a fresh code on the spot). Trust root remains SECRET confidentiality + TG channel security.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import pathlib
import struct
import time

CODE_MAX_AGE = 86400          # time-limited code validity: 24h (code-expiry policy)
CLOCK_SKEW = 60               # tolerate 60s clock skew (guards against minor signing/verification clock drift)
DOM = os.environ.get("DASH_DOMAIN", "your-domain.example.com")  # set DASH_DOMAIN to your own host in production
DEVICE_COOKIE_MAX_AGE = 90 * 86400   # device cookie validity: 90 days (re-tap a TG link once on expiry)
COOKIE_NAME = "__Host-dash_device"   # __Host- prefix enforces Secure+Path=/+no Domain (OWASP cookie convention)
_COOKIE_CTX = b"device-cookie-v1"    # signing domain-separation prefix: cookie != code, mutual non-impersonation


def _secret() -> bytes:
    """Resolve SECRET: env var takes precedence; fallback reads the dashboard .env
    (same-host 600 perms, shared source with app.py, never in SKILL/git)."""
    s = os.environ.get("DASHBOARD_SECRET")
    if s:
        return s.encode()
    env = pathlib.Path(os.environ.get("DASH_ENV", os.path.expanduser("~/apps/btc-dashboard/.env")))
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DASHBOARD_SECRET="):
                return line.partition("=")[2].strip().encode()
    return b"dev-secret-change-me"


def static_token(secret: bytes | None = None) -> str:
    """Static identity token (matches historical implementation; backward-compatible)."""
    return hmac.new(secret or _secret(), b"btc-dashboard-v1", hashlib.sha256).hexdigest()[:24]


def _sign(msg: bytes, secret: bytes) -> bytes:
    return hmac.new(secret, msg, hashlib.sha256).digest()[:16]   # 128-bit truncation, sufficient for forgery resistance


def _make_signed(sec: bytes, ctx: bytes, now: float | None) -> str:
    """Issue: base64url(8B timestamp + 16B HMAC(ctx+timestamp)), no padding. ctx provides domain separation."""
    ts = int(now if now is not None else time.time())
    msg = struct.pack(">Q", ts)
    return base64.urlsafe_b64encode(msg + _sign(ctx + msg, sec)).decode().rstrip("=")


def _verify_signed(value: str | None, sec: bytes, ctx: bytes, max_age: int, now: float | None) -> bool:
    """Verify: HMAC(ctx+timestamp) is valid and within max_age.
    Different ctx values are non-interchangeable (domain separation)."""
    value = value or ""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        msg, sig = raw[:8], raw[8:]
        if len(msg) != 8 or not hmac.compare_digest(sig, _sign(ctx + msg, sec)):
            return False
        ts = struct.unpack(">Q", msg)[0]
        age = int(now if now is not None else time.time()) - ts
        return -CLOCK_SKEW <= age <= max_age
    except Exception:
        return False


def make_code(secret: bytes | None = None, now: float | None = None) -> str:
    """Issue a fresh time-limited code valid for 24h. Empty ctx string keeps the signature format
    identical to the historical format so existing issued links remain valid."""
    return _make_signed(secret or _secret(), b"", now)


def verify(token: str, code: str, secret: bytes | None = None, now: float | None = None) -> bool:
    """Dual-factor: timing-safe compare of static token AND valid time-limited code
    (HMAC signature valid and within 24h). Either failing → False."""
    sec = secret or _secret()
    if not hmac.compare_digest(token or "", static_token(sec)):
        return False
    return _verify_signed(code, sec, b"", CODE_MAX_AGE, now)


def make_device_cookie(secret: bytes | None = None, now: float | None = None) -> str:
    """Issue a fresh device cookie valid for 90 days (device-trust policy).
    Domain-separation prefix — possessing a code cannot impersonate a cookie and vice versa."""
    return _make_signed(secret or _secret(), _COOKIE_CTX, now)


def verify_device_cookie(value: str | None, secret: bytes | None = None, now: float | None = None) -> bool:
    """Verify a device cookie: HMAC valid and within 90 days. The signature embeds both identity
    and expiry layers; a single value is sufficient for authorization."""
    return _verify_signed(value, secret or _secret(), _COOKIE_CTX, DEVICE_COOKIE_MAX_AGE, now)


def dash_url(mstr: bool = False, secret: bytes | None = None) -> str:
    """Issue a complete dashboard URL (static token + time-limited code).
    Used by show_progress / record_fill / SKILL to send links."""
    sec = secret or _secret()
    q = f"token={static_token(sec)}&code={make_code(sec)}"
    if mstr:
        q = "type=mstr&" + q
    return f"https://{DOM}/d?{q}"


if __name__ == "__main__":
    import sys
    # WARNING: output contains token+code credentials; for manual/interactive use only.
    # Do not redirect to disk, do not run in CI/cron/set -x.
    print(dash_url(mstr="--mstr" in sys.argv))
