"""
test_app_tier_overlay.py — app.build_dashboard main-table trigger-price overlay unit tests (standalone).

Scope: app.build_dashboard overlays B3/C1/C2 open-leg plan[i].price with dynamic trigger prices
       (legs pre-aligned: B3 three legs / C1·C2 two legs; rmb amounts unchanged).
       B1/B2 are not overlaid (already filled; main table shows real fill prices).

Invariants tested (display-only, never writes state):
  A-1 B3/C1/C2 open legs -> plan.price = dynamic trigger price (green/wma200/realized + per-tier offsets)
  A-2 rmb amounts not modified (only trigger price is overridden)
  A-3 B1/B2 not overlaid (filled tiers; plan passed through as-is)
  A-4 anchor fail-closed (green line missing) -> that tier not overlaid, plan unchanged (no garbage prices)
  A-5 after run state.json not written: original st in-memory plan.price fully unchanged (deep-copy mutation only)

Run: PYTHONPATH=app python3 app/test_app_tier_overlay.py (all green -> exit 0).
Fully monkeypatched (no network, no real cache/ledger); state from seeds/state.seed.json fixture.
"""

import copy
import json
import os
import pathlib
import sys
import types

# app.py top-level imports Flask for routes only; build_dashboard does not touch Flask.
# Tests run under plain python3 (no Flask dependency, consistent with run-tests.sh) ->
# inject a minimal Flask stub so the top-level import succeeds; never pull in real Flask
# (pure-function tests do not need a web framework).
if "flask" not in sys.modules:
    _dec = lambda *aa, **kk: (lambda f: f)  # no-op decorator factory: @app.get/.post/.route/.errorhandler

    class _FlaskStub:
        def __init__(self, *a, **k):
            self.get = _dec
            self.post = _dec
            self.route = _dec
            self.errorhandler = _dec

    _stub = types.ModuleType("flask")
    _stub.Flask = _FlaskStub
    _stub.request = None
    _stub.send_file = lambda *a, **k: None
    _stub.jsonify = lambda *a, **k: None
    _stub.abort = lambda *a, **k: None
    sys.modules["flask"] = _stub

import app as A  # noqa: E402
import onchain_signals  # noqa: E402
import tier_alert  # noqa: E402

# Save real get_usdt_cny_rate before _patch replaces it; a06+ restores the real function to test network fallback.
_REAL_RATE_FN = A.get_usdt_cny_rate

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


HERE = pathlib.Path(__file__).resolve().parent
SEED = HERE.parent / "seeds" / "state.seed.json"


def _seed_state():
    """Load the seed ledger as a fixture (read-only; never touches the real ledger)."""
    return json.loads(SEED.read_text(encoding="utf-8"))


# Fixed anchor values: green=60822 / wma200=61573 / realized=53575 (per-tier offsets + leg counts)
def _cache(wma200=61_573.0, realized=53_575.0):
    data = {"wma200": wma200, "realized_price": realized,
            "green_line": None, "green_line_source": "manual"}
    return {"brk": {"ok": True, "fetched_at": "2026-06-09T08:00:00+10:00", "data": data}}


def _patch(spot=63_000.0, cache=None, green=(60_822.0, False), klines=None, rate=(6.81, "binance")):
    """Monkeypatch app's price/cache/green-line/klines/rate dependencies to prevent network calls and real-file access."""
    A.get_spot = lambda: spot
    A.get_klines_30d = lambda: (klines if klines is not None else [70_000.0] * 30)
    A.get_usdt_cny_rate = lambda: rate
    onchain_signals.load_cache = lambda *a, **k: (cache if cache is not None else _cache())
    tier_alert.manual_green = lambda *a, **k: green


def _tier(payload, code):
    return next(t for t in payload["tiers"] if str(t.get("name", "")).startswith(code))


def _by_code(state, code):
    return next(t for t in state["tiers"] if str(t.get("name", "")).startswith(code))


# ════════════════════════════════════════════════════════════════════
# A-1/A-2/A-3 normal anchors -> B3/C1/C2 open legs overlaid, amounts unchanged, B1/B2 not overlaid
# ════════════════════════════════════════════════════════════════════
print("▶ a01_overlay_dynamic: B3/C1/C2 open legs plan.price = dynamic trigger price")
st = _seed_state()
_patch(spot=63_000.0)
payload = A.build_dashboard(st)

b3_plan = _tier(payload, "B3").get("plan", [])
c1_plan = _tier(payload, "C1").get("plan", [])
c2_plan = _tier(payload, "C2").get("plan", [])
check("B3 plan.price = [60822,58822,56822] (3 legs, anchor green 60822)",
      [p["price"] for p in b3_plan] == [60_822.0, 58_822.0, 56_822.0], str([p["price"] for p in b3_plan]))
check("C1 plan.price = [57573,55573] (2 legs, anchor wma200 61573)",
      [p["price"] for p in c1_plan] == [57_573.0, 55_573.0], str([p["price"] for p in c1_plan]))
check("C2 plan.price = [54575,51575] (2 legs, anchor realized 53575)",
      [p["price"] for p in c2_plan] == [54_575.0, 51_575.0], str([p["price"] for p in c2_plan]))

print("▶ a02_amount_untouched: trigger price overlaid only, rmb amount unchanged")
seed = _seed_state()
check("B3 rmb matches seed (price overridden, amount unchanged)",
      [p["rmb"] for p in b3_plan] == [p["rmb"] for p in _by_code(seed, "B3")["plan"]],
      str([p["rmb"] for p in b3_plan]))
check("C1 rmb matches seed",
      [p["rmb"] for p in c1_plan] == [p["rmb"] for p in _by_code(seed, "C1")["plan"]])
check("C2 rmb matches seed",
      [p["rmb"] for p in c2_plan] == [p["rmb"] for p in _by_code(seed, "C2")["plan"]])

print("▶ a03_b1b2_not_overlaid: B1/B2 filled tiers not overlaid (main table shows real fills)")
b1_plan = _tier(payload, "B1").get("plan", [])
b2_plan = _tier(payload, "B2").get("plan", [])
check("B1 plan.price matches seed (no overlay)",
      [p["price"] for p in b1_plan] == [p["price"] for p in _by_code(seed, "B1")["plan"]],
      str([p["price"] for p in b1_plan]))
check("B2 plan.price matches seed (no overlay)",
      [p["price"] for p in b2_plan] == [p["price"] for p in _by_code(seed, "B2")["plan"]],
      str([p["price"] for p in b2_plan]))
# B1 already filled: main table fills still carry the real fill price (overlay only touches plan, not fills)
check("B1 fills real fill price passed through unchanged",
      [f["price"] for f in _tier(payload, "B1").get("fills", [])]
      == [f["price"] for f in _by_code(seed, "B1")["fills"]])


# ════════════════════════════════════════════════════════════════════
# A-4 anchor fail-closed (green line missing) -> that tier not overlaid, plan unchanged
# ════════════════════════════════════════════════════════════════════
print("▶ a04_failclosed_no_overlay: green line missing -> B3 not overlaid (plan unchanged), C1/C2 still overlaid")
st2 = _seed_state()
_patch(spot=63_000.0, green=(None, False))  # green line missing -> B3 triggers None
payload2 = A.build_dashboard(st2)
b3_plan2 = _tier(payload2, "B3").get("plan", [])
check("green line missing -> B3 plan.price unchanged (no invalid overlay)",
      [p["price"] for p in b3_plan2] == [p["price"] for p in _by_code(seed, "B3")["plan"]],
      str([p["price"] for p in b3_plan2]))
# wma200/realized still present -> C1/C2 overlaid as usual (green-line failure does not cascade)
check("C1 still overlaid (green-line failure does not cascade)",
      [p["price"] for p in _tier(payload2, "C1").get("plan", [])] == [57_573.0, 55_573.0])
check("C2 still overlaid (green-line failure does not cascade)",
      [p["price"] for p in _tier(payload2, "C2").get("plan", [])] == [54_575.0, 51_575.0])


# ════════════════════════════════════════════════════════════════════
# A-5 state.json not written: original in-memory st plan.price fully unchanged (deep-copy mutation only)
# ════════════════════════════════════════════════════════════════════
print("▶ a05_no_state_write: overlay mutates payload copy only; original st unchanged, no state.json written")
st3 = _seed_state()
before = copy.deepcopy(st3)
_patch(spot=63_000.0)
_here = HERE
_state_before = set(__import__("glob").glob(str(_here / "**" / "*state*.json"), recursive=True))
A.build_dashboard(st3)
check("original st.tiers plan.price fully unchanged (copy-only mutation)",
      st3["tiers"] == before["tiers"], "st mutated!")
_state_after = set(__import__("glob").glob(str(_here / "**" / "*state*.json"), recursive=True))
check("no new state.json written to app dir (display-only)",
      _state_after == _state_before, f"new={_state_after - _state_before}")
check("no state.json written in cwd",
      not os.path.exists(os.path.join(os.getcwd(), "state.json")))


# ════════════════════════════════════════════════════════════════════
# Real-time ¥/USDT reference rate (Binance P2P; exception -> fallback RATE, no fabrication)
# ════════════════════════════════════════════════════════════════════
print("▶ a06_rate_binance: P2P success -> rate=6.81 source=binance")
# _patch replaced A.get_usdt_cny_rate with a stub; restore the real function to test the network layer fallback.
A.get_usdt_cny_rate = _REAL_RATE_FN
_orig_post = A._post
A._post = lambda url, payload: {"data": [{"adv": {"price": "6.81"}}]}
rate, src = A.get_usdt_cny_rate()
check("P2P best-sell quote parsed -> rate=6.81", rate == 6.81, str(rate))
check("source=binance", src == "binance", src)

print("▶ a07_rate_fallback: P2P raises exception -> fallback=RATE no crash, source=fallback")
def _boom(*a, **k):
    raise RuntimeError("p2p down")
A._post = _boom
rate_f, src_f = A.get_usdt_cny_rate()
check("exception -> fallback to RATE (no fabrication)", rate_f == A.RATE, f"{rate_f} vs {A.RATE}")
check("exception -> source=fallback", src_f == "fallback", src_f)

print("▶ a08_rate_out_of_range: P2P returns out-of-range value (e.g. 99.9) -> fallback (no absurd value used for display)")
A._post = lambda url, payload: {"data": [{"adv": {"price": "99.9"}}]}
rate_o, src_o = A.get_usdt_cny_rate()
check("out-of-range rate -> fallback to RATE", rate_o == A.RATE and src_o == "fallback", f"{rate_o}/{src_o}")
A._post = _orig_post

print("▶ a09_payload_rate: build_dashboard payload carries rate + rate_source")
st_r = _seed_state()
_patch(spot=63_000.0, rate=(6.81, "binance"))
pay_r = A.build_dashboard(st_r)
check("payload.rate=6.81", pay_r.get("rate") == 6.81, str(pay_r.get("rate")))
check("payload.rate_source=binance", pay_r.get("rate_source") == "binance", str(pay_r.get("rate_source")))


# ════════════════════════════════════════════════════════════════════
# Cycle clock: build_dashboard injects cycle (read-only) + backward compat when cache missing
# ════════════════════════════════════════════════════════════════════
print("▶ a10_cycle_injected: cache has cycle -> payload.cycle.halving has est_date + bear-low constant")
st_c = _seed_state()
cache_cyc = {**_cache(), "cycle": {"ok": True, "fetched_at": "2026-06-10T01:00:00+10:00",
             "data": {"height": 953036, "halving_block": 1_050_000,
                      "blocks_remaining": 96964, "est_date": "2028-04-15"}}}
_patch(spot=63_000.0, cache=cache_cyc)
cyc = A.build_dashboard(st_c).get("cycle") or {}
check("payload contains cycle dict", isinstance(cyc, dict) and bool(cyc))
check("bear_low_date = user-estimated 2026-10-05", cyc.get("bear_low_date") == "2026-10-05")
check("bear-low constant matches A.BEAR_LOW_DATE", cyc.get("bear_low_date") == A.BEAR_LOW_DATE)
hv = cyc.get("halving") or {}
check("halving.est_date injected", hv.get("est_date") == "2028-04-15")
check("halving.blocks_remaining injected", hv.get("blocks_remaining") == 96964)
check("halving.height injected", hv.get("height") == 953036)
check("halving.fetched_at passed through (frontend staleness indicator)", hv.get("fetched_at") == "2026-06-10T01:00:00+10:00")

print("▶ a11_cycle_backward_compat: no cycle in cache -> halving=None, bear-low still present (independent of cron)")
st_c2 = _seed_state()
_patch(spot=63_000.0)   # default _cache() has no cycle section (= old cache or first-day cron not yet run)
cyc2 = A.build_dashboard(st_c2).get("cycle") or {}
check("no cycle cache -> halving is None (frontend shows 'estimate updating', not blank)", cyc2.get("halving") is None)
check("bear-low estimate still injected (independent of cron)", cyc2.get("bear_low_date") == "2026-10-05")

print("▶ a12_cycle_partial_cache: cycle section has no est_date (partial cache) -> halving=None, no garbage rendered")
st_c3 = _seed_state()
cache_partial = {**_cache(), "cycle": {"ok": False, "fetched_at": "", "data": None}}
_patch(spot=63_000.0, cache=cache_partial)
cyc3 = A.build_dashboard(st_c3).get("cycle") or {}
check("cycle.data is None -> halving is None", cyc3.get("halving") is None)
check("bear-low still present", cyc3.get("bear_low_date") == "2026-10-05")


# ════════════════════════════════════════════════════════════════════
# Cycle low + diminishing-returns indicator: build_dashboard injects cycle_low/decay (read-only)
# ════════════════════════════════════════════════════════════════════
print("▶ a13_cycle_low_injected: cache has cycle_low -> payload.cycle_low + payload.decay both populated")
st_l = _seed_state()
cache_cl = {**_cache(), "cycle_low": {"ok": True, "fetched_at": "2026-07-02T01:00:00+10:00",
            "data": {"low": 57800.19, "low_date": "2026-07-01", "ath": 126199.63, "ath_date": "2025-10-06"}}}
_patch(spot=63_400.0, cache=cache_cl)
pay_l = A.build_dashboard(st_l)
cl = pay_l.get("cycle_low") or {}
check("payload contains cycle_low dict", isinstance(cl, dict) and bool(cl))
check("cycle_low.low passed through", cl.get("low") == 57800.19)
check("cycle_low.low_date passed through", cl.get("low_date") == "2026-07-01")
check("cycle_low.ath passed through", cl.get("ath") == 126199.63)
check("cycle_low.fetched_at passed through (frontend staleness indicator)", cl.get("fetched_at") == "2026-07-02T01:00:00+10:00")
dc = pay_l.get("decay") or {}
check("payload contains decay dict", isinstance(dc, dict) and bool(dc))
check("decay.low == cycle_low.low", dc.get("low") == round(57800.19))
check("decay.spot == live spot (63,400)", dc.get("spot") == 63_400)
check("decay zone reflects spot vs low (spot well above low -> not below_low)", dc.get("zone") != "below_low")

print("▶ a14_cycle_low_backward_compat: no cycle_low in cache -> cycle_low/decay both None (never fabricated)")
st_l2 = _seed_state()
_patch(spot=63_400.0)   # default _cache() has no cycle_low section (= old cache or first-day cron not yet run)
pay_l2 = A.build_dashboard(st_l2)
check("no cycle_low cache -> cycle_low is None", pay_l2.get("cycle_low") is None)
check("no cycle_low cache -> decay is None (never computed from a fabricated low)", pay_l2.get("decay") is None)

print("▶ a15_cycle_low_partial_cache: cycle_low section has no low (fetch failed) -> cycle_low/decay both None")
st_l3 = _seed_state()
cache_cl_bad = {**_cache(), "cycle_low": {"ok": False, "fetched_at": "", "data": None}}
_patch(spot=63_400.0, cache=cache_cl_bad)
pay_l3 = A.build_dashboard(st_l3)
check("cycle_low.data is None -> cycle_low is None", pay_l3.get("cycle_low") is None)
check("cycle_low.data is None -> decay is None", pay_l3.get("decay") is None)


print(f"\n{'=' * 44}\nPassed {PASS} · Failed {FAIL}")
import sys  # noqa: E402
sys.exit(1 if FAIL else 0)
