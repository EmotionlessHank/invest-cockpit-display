"""
test_dash_link.py — dash_link dual-factor authentication tests (pure stdlib, in-process;
time injected via `now` parameter — no wall-clock dependency).
Run: PYTHONPATH=app python3 app/test_dash_link.py (run-tests.sh sets PYTHONPATH). All green → exit 0.
"""

import os
import sys

os.environ["DASHBOARD_SECRET"] = "test-secret-fixed"   # fixed secret, avoids reading .env

import dash_link as dl   # noqa: E402

PASS = FAIL = 0
SEC = b"test-secret-fixed"
T0 = 1_000_000


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


print("▶ static_token: 24-char hex, deterministic")
tok = dl.static_token(SEC)
check("24-char hex", len(tok) == 24 and all(c in "0123456789abcdef" for c in tok), tok)
check("deterministic", tok == dl.static_token(SEC))

print("▶ make_code / verify roundtrip")
code = dl.make_code(SEC, now=T0)
check("valid dual-factor passes", dl.verify(tok, code, SEC, now=T0))

print("▶ both factors required")
check("wrong token rejected", not dl.verify("bad", code, SEC, now=T0))
check("wrong code rejected", not dl.verify(tok, "bad", SEC, now=T0))
check("empty token rejected", not dl.verify("", code, SEC, now=T0))
check("empty code rejected", not dl.verify(tok, "", SEC, now=T0))

print("▶ tampered code")
flip = code[:-1] + ("A" if code[-1] != "A" else "B")
check("last-char flip invalidates code", not dl.verify(tok, flip, SEC, now=T0))

print("▶ time window (24h + 60s clock skew)")
check("valid within 23h59m", dl.verify(tok, code, SEC, now=T0 + 86340))
check("valid at exactly 24h boundary", dl.verify(tok, code, SEC, now=T0 + 86400))
check("expired past 24h", not dl.verify(tok, code, SEC, now=T0 + 86401))
check("60s clock skew tolerated", dl.verify(tok, code, SEC, now=T0 - 60))
check("future timestamp beyond 60s rejected", not dl.verify(tok, code, SEC, now=T0 - 61))

print("▶ different secrets are mutually exclusive")
check("token+code signed by another secret rejected",
      not dl.verify(dl.static_token(b"other"), dl.make_code(b"other", now=T0), SEC, now=T0))

print("▶ malformed input does not crash")
check("garbage code does not raise", dl.verify(tok, "!!!not-base64!!!", SEC, now=T0) is False)
check("None input is safe", dl.verify(None, None, SEC, now=T0) is False)

print("▶ code signature format is backward-compatible (regression guard: existing links remain valid)")
import base64 as _b64, hashlib as _hl, hmac as _hm  # noqa: E402
_raw = _b64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
check("timestamp+HMAC(msg) legacy format matches", _raw[8:] == _hm.new(SEC, _raw[:8], _hl.sha256).digest()[:16])

print("▶ device cookie (90-day expiry + domain separation)")
ck = dl.make_device_cookie(SEC, now=T0)
check("valid cookie passes", dl.verify_device_cookie(ck, SEC, now=T0))
check("valid within 89 days", dl.verify_device_cookie(ck, SEC, now=T0 + 89 * 86400))
check("valid at exactly 90-day boundary", dl.verify_device_cookie(ck, SEC, now=T0 + dl.DEVICE_COOKIE_MAX_AGE))
check("expired past 90 days", not dl.verify_device_cookie(ck, SEC, now=T0 + dl.DEVICE_COOKIE_MAX_AGE + 1))
check("60s clock skew tolerated", dl.verify_device_cookie(ck, SEC, now=T0 - 60))
check("domain separation: code cannot impersonate cookie", not dl.verify_device_cookie(code, SEC, now=T0))
check("domain separation: cookie cannot impersonate code", not dl.verify(tok, ck, SEC, now=T0))
_flip = ck[:-1] + ("A" if ck[-1] != "A" else "B")
check("tampered cookie rejected", not dl.verify_device_cookie(_flip, SEC, now=T0))
check("cookie signed by another secret rejected", not dl.verify_device_cookie(dl.make_device_cookie(b"other", now=T0), SEC, now=T0))
check("garbage input does not crash", dl.verify_device_cookie("!!!not-base64!!!", SEC, now=T0) is False)
check("None input is safe", dl.verify_device_cookie(None, SEC, now=T0) is False)
check("COOKIE_NAME enforces __Host- prefix convention", dl.COOKIE_NAME.startswith("__Host-"))

print("▶ dash_url format")
u = dl.dash_url(secret=SEC)
m = dl.dash_url(mstr=True, secret=SEC)
check("BTC URL contains token&code, no type param", u.startswith("https://") and "token=" in u and "code=" in u and "type=mstr" not in u)
check("MSTR URL contains type=mstr", m.startswith("https://") and "type=mstr" in m and "code=" in m)

print("▶ _secret priority (env takes precedence)")
check("env var takes precedence", dl._secret() == b"test-secret-fixed")

print(f"\n{'=' * 40}\nPassed {PASS} · Failed {FAIL}")
sys.exit(1 if FAIL else 0)
