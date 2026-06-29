"""
test_app_record.py — /api/record route-level tests (btc_fill label/usdt passthrough and
first-pass validation).

Driver: Flask test_client hits the real route -> route subprocesses the real record_fill.py ->
assertions check both the response JSON and the on-disk tempfile state.
State always points to a tempfile (tests never touch the local seed or real ledger).
Run via ./run-tests.sh (requires PYTHONPATH=app so scripts can import subcode).
"""

import copy
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).parent
_TMP = tempfile.mkdtemp(prefix="dd014-app-record-")
STATE_PATH = pathlib.Path(_TMP) / "state.json"
# Must be set before importing app (app.py reads env at module level)
os.environ["DASHBOARD_SECRET"] = "test-secret-dd014"
os.environ["BTC_STATE"] = str(STATE_PATH)
sys.path.insert(0, str(HERE))

import app as appmod  # noqa: E402
import dash_link      # noqa: E402

BASE = {
    "updated": "2026-06-03T00:00:00+10:00",
    "held_btc": 0.1, "invested_rmb": 50000, "avg_cost_usd": 68000,
    "budget_total_rmb": 200000, "rate_rmb_per_usdt": 7.0,
    "tracks": [
        {"name": "Total budget used", "used": 50000, "quota": 200000, "color": "green"},
        {"name": "DCA", "used": 3000, "quota": 30000, "color": "blue"},
        {"name": "B core dip-buy", "used": 0, "quota": 88000, "color": "amber"},
        {"name": "C deep-dip reserve", "used": 0, "quota": 30135, "color": "red"},
    ],
    "tiers": [
        {"name": "B2 core", "lo": 60000, "hi": 66000, "amount": 28000, "status": "open", "sem": "amber",
         "plan": [{"label": "B2-a", "price": 64000, "rmb": 14000, "status": "open"},
                  {"label": "B2-b", "price": 61000, "rmb": 14000, "status": "open"}], "fills": []},
        {"name": "C2 deep dip", "lo": 48000, "hi": 52000, "amount": 14135, "status": "wait", "sem": "red",
         "plan": [{"label": "C2-a", "price": 51000, "rmb": 7000, "status": "open"},
                  {"label": "C2-b", "price": 48000, "rmb": 7000, "status": "open"}], "fills": []},
    ],
    "onchain": [],
}

PASS = FAIL = 0
client = appmod.app.test_client()


def fresh():
    STATE_PATH.write_text(json.dumps(copy.deepcopy(BASE), ensure_ascii=False), "utf-8")


def state():
    return json.loads(STATE_PATH.read_text("utf-8"))


def post(body, with_auth=True):
    if with_auth:
        body = {**body, "token": dash_link.static_token(), "code": dash_link.make_code()}
    return client.post("/api/record", json=body)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def t_b1_label_passthrough():
    """B1 valid label passthrough: cross-tier price + label=C2-a -> route-level fill recorded in C2, track C charged (full-stack)"""
    fresh()
    d = post({"kind": "btc_fill", "label": "C2-a", "price": 61000}).get_json()
    check("ok and claimed C2 deep dip", d.get("ok") is True and d.get("claimed") == "C2 deep dip", d)
    st = state()
    check("fill recorded in C2.fills", len(st["tiers"][1]["fills"]) == 1 and not st["tiers"][0]["fills"], st["tiers"])
    check("track C +7000 / B unchanged",
          st["tracks"][3]["used"] == 7000 and st["tracks"][2]["used"] == 0, st["tracks"])


def t_b2_label_format_whitelist():
    """B2 invalid label formats (flag-like / injection / oversized) -> app first-pass rejection, no subprocess, no disk write"""
    fresh()
    before = STATE_PATH.read_text("utf-8")
    for bad in ("-x", "B2z", "../etc", "B2-a; rm", "ZZ9-aa"):
        d = post({"kind": "btc_fill", "label": bad, "price": 61000}).get_json()
        check(f"label '{bad}' rejected", d.get("ok") is False and "invalid" in d.get("msg", ""), d)
    check("ledger unchanged", STATE_PATH.read_text("utf-8") == before)


def t_b3_usdt():
    """B3 USDT passthrough and mutual exclusion: usdt-only ok (converts to ¥14000); usdt+rmb both given rejected; usdt out of range rejected"""
    fresh()
    d = post({"kind": "btc_fill", "label": "B2-a", "price": 64000, "usdt": 2000}).get_json()
    check("usdt=2000 -> rmb 14000 recorded", d.get("ok") is True and d.get("rmb") == 14000, d)
    d2 = post({"kind": "btc_fill", "label": "B2-b", "price": 61000, "usdt": 2000, "rmb": 14000}).get_json()
    check("usdt+rmb both given -> exclusive, rejected", d2.get("ok") is False and "exclusive" in d2.get("msg", ""), d2)
    d3 = post({"kind": "btc_fill", "label": "B2-b", "price": 61000, "usdt": 50000}).get_json()
    check("usdt out of range -> rejected", d3.get("ok") is False, d3)


def t_b4_old_body_regression():
    """B4 legacy body (price/rmb only, no label/usdt) -> behavior regression unchanged"""
    fresh()
    d = post({"kind": "btc_fill", "price": 64000, "rmb": 5000}).get_json()
    check("legacy path ok and nearest-price claim B2-a", d.get("ok") is True and d.get("label") == "B2-a", d)
    check("invested +5000", state()["invested_rmb"] == 55000, state()["invested_rmb"])


def t_b5_auth_fail():
    """Write-route auth failure -> fake 404 (existence not revealed)"""
    fresh()
    r = post({"kind": "btc_fill", "label": "B2-a", "price": 64000}, with_auth=False)
    check("no credentials -> 404", r.status_code == 404, r.status_code)
    check("no disk write", not state()["tiers"][0]["fills"])


if __name__ == "__main__":
    for fn in (t_b1_label_passthrough, t_b2_label_format_whitelist, t_b3_usdt,
               t_b4_old_body_regression, t_b5_auth_fail):
        print(f"▶ {fn.__name__}: {fn.__doc__.strip()}")
        fn()
    print(f"\n{'=' * 40}\nPassed {PASS} · Failed {FAIL}")
    sys.exit(1 if FAIL else 0)
