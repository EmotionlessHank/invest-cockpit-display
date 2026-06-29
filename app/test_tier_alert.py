"""
test_tier_alert.py — Dip tier dynamic display + price alert (dedup) unit tests (standalone).

Coverage: tier_alert.build_tier_view (display-derived, pure function)
        + tier_alert.alert_decisions (alert decision, semi-pure, double dedup + fail-toward-alert).

Invariants tested (real-money scenario):
  L3-1 Dynamic trigger prices + dist % follow cache changes (anchor drift → display prices follow)
  L3-4 Filled tiers are never alerted (reads state.json tiers[].fills, read-only)
  L3-5 alert_state missing → fail-toward-alert: alert fires once (never silently suppressed)
  L3-6 L2 fail-closed tier → display "Data unavailable" and not alertable
  Dedup: same tier same day does not repeat
  Isolation: no state.json write, no record_fill call (three-layer isolation is non-negotiable)

Run: PYTHONPATH=app python3 app/test_tier_alert.py (all green → exit 0).
Uses dict fixtures throughout; no network, no real ledger; alert_state written to /tmp.
"""

import glob
import os
import pathlib
import tempfile

import strategy as S
import tier_alert as TA

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def _cache(wma200=61_000.0, realized=53_600.0, extra=None):
    """L1 cache shape: cache['brk']['data'] contains anchors + fetched_at (for provenance)."""
    data = {"wma200": wma200, "realized_price": realized,
            "green_line": None, "green_line_source": "manual"}
    if extra:
        data.update(extra)
    return {"brk": {"ok": True, "fetched_at": "2026-06-09T08:00:00+10:00", "data": data}}


def _state(fills_by_code=None):
    """state.json shape: tier names prefixed B1/B2/B3/C1/C2; optional fills injection per tier."""
    fills_by_code = fills_by_code or {}
    names = {"B1": "B1 Probe", "B2": "B2 Core", "B3": "B3 Green Line", "C1": "C1 Deep Dip", "C2": "C2 Deep Dip"}
    tiers = []
    for code, name in names.items():
        tiers.append({"name": name, "lo": 0, "hi": 0, "status": "open",
                      "plan": [], "fills": list(fills_by_code.get(code, []))})
    return {"tiers": tiers, "invested_rmb": 50_000}


def _get_view(spot, cache=None, state=None, manual_green=60_000.0, now=None):
    return TA.build_tier_view(cache or _cache(), state or _state(),
                              manual_green=manual_green, spot=spot, now=now)


def _tier(view, code):
    return next(t for t in view["tiers"] if t["code"] == code)


# ════════════════════════════════════════════════════════════════════
# L3-1 Dynamic trigger prices + dist % follow cache anchor changes
# ════════════════════════════════════════════════════════════════════
print("▶ t01_dynamic_triggers: trigger prices + dist % follow cache anchors")
# green=60K → B3=[60000,58000,56000]; wma200=61K → C1=[61000,59000,57000]
v1 = _get_view(spot=62_000.0, cache=_cache(wma200=61_000.0, realized=53_600.0), manual_green=60_000.0)
b3 = _tier(v1, "B3")
check("B3 trigger prices = [60000,58000,56000]", b3["triggers"] == [60_000.0, 58_000.0, 56_000.0], str(b3["triggers"]))
check("B3 display contains $60,000", "$60,000" in b3["display"], b3["display"])
# spot 62K is above B3 → not in zone
check("spot 62K not in B3 zone (in_zone=False)", b3["in_zone"] is False)
check("B3 dist_pct positive when spot is above top trigger", b3["dist_pct"] is not None and b3["dist_pct"] > 0, str(b3["dist_pct"]))

# Anchor drift: green line drops to 56K → B3 trigger prices shift down; dist % changes for same spot
v2 = _get_view(spot=62_000.0, cache=_cache(wma200=61_000.0, realized=53_600.0), manual_green=56_000.0)
b3b = _tier(v2, "B3")
check("green line shifted to 56K → B3=[56000,54000,52000] (display follows anchor)",
      b3b["triggers"] == [56_000.0, 54_000.0, 52_000.0], str(b3b["triggers"]))
check("anchor change → dist % changes for same spot", b3["dist_pct"] != b3b["dist_pct"], f"{b3['dist_pct']} vs {b3b['dist_pct']}")

print("▶ t02_in_zone_distpct: spot in zone → in_zone + dist % to nearest leg")
# green=60K B3=[60000,58000,56000], spot 57K → in zone, nearest leg below spot = 56000
v3 = _get_view(spot=57_000.0, manual_green=60_000.0)
b3c = _tier(v3, "B3")
check("spot 57K in B3 zone (≤60000)", b3c["in_zone"] is True)
check("nearest trigger leg = 56000 (deepest leg below spot)", b3c["nearest"] == 56_000.0, str(b3c["nearest"]))
check("dist_pct = (57000-56000)/56000*100 ≈ 1.79", abs(b3c["dist_pct"] - 1.79) < 0.01, str(b3c["dist_pct"]))


# ════════════════════════════════════════════════════════════════════
# L3-6 L2 fail-closed tier → display "Data unavailable" and not alertable
# ════════════════════════════════════════════════════════════════════
print("▶ t03_failclosed_display: L2 fail-closed tier → Data unavailable + not alertable")
# green line missing (manual_green=None) → B3 triggers=None
v_fc = _get_view(spot=57_000.0, manual_green=None)
b3_fc = _tier(v_fc, "B3")
check("missing green line → B3 triggers None", b3_fc["triggers"] is None)
check("B3 display = Data unavailable", b3_fc["display"] == TA.DISPLAY_UNAVAILABLE, b3_fc["display"])
check("B3 alertable=False (fail-closed excluded from alerts)", b3_fc["alertable"] is False)
check("B3 in_zone=False (no price, cannot judge zone)", b3_fc["in_zone"] is False)
# wma200 out of range → C1 fail-closed
v_oor = _get_view(spot=57_000.0, cache=_cache(wma200=5_000.0, realized=53_600.0), manual_green=60_000.0)
c1_oor = _tier(v_oor, "C1")
check("wma200 out of range → C1 Data unavailable", c1_oor["display"] == TA.DISPLAY_UNAVAILABLE)
check("wma200 out of range → C1 not alertable", c1_oor["alertable"] is False)
check("degraded list contains wma200_unavailable", "wma200_unavailable" in v_oor["degraded"], str(v_oor["degraded"]))
# B3 is not affected by C1's degradation
check("degradation is not contagious: B3 still has trigger prices", _tier(v_oor, "B3")["triggers"] is not None)

print("▶ t04_failclosed_no_alert: fail-closed tier never enters to_alert")
to_alert, _ = TA.alert_decisions(v_fc, _state(), {}, now=None)
check("missing green line → to_alert does not contain B3", all(a["code"] != "B3" for a in to_alert), str([a["code"] for a in to_alert]))


# ════════════════════════════════════════════════════════════════════
# L3-4 Filled tiers are never alerted (reads state.json tiers[].fills, read-only)
# ════════════════════════════════════════════════════════════════════
print("▶ t05_filled_no_alert: filled tiers are never alerted")
# spot 57K is in B3 zone, but B3 has a fill → not alerted
st_filled = _state(fills_by_code={"B3": [{"price": 58_000, "rmb": 16000, "at": "2026-06-08T10:00:00+10:00"}]})
v_filled = TA.build_tier_view(_cache(), st_filled, manual_green=60_000.0, spot=57_000.0)
b3_filled = _tier(v_filled, "B3")
check("B3 filled=True (reads state.fills)", b3_filled["filled"] is True)
check("B3 filled → alertable=False", b3_filled["alertable"] is False)
to_alert_f, _ = TA.alert_decisions(v_filled, st_filled, {}, now=None)
check("filled B3 → not in to_alert", all(a["code"] != "B3" for a in to_alert_f), str([a["code"] for a in to_alert_f]))
# Unfilled tier (C2, spot 57K in zone) still alerts — control case
v_full = TA.build_tier_view(_cache(), st_filled, manual_green=60_000.0, spot=57_000.0)
codes_alerted = {a["code"] for a in TA.alert_decisions(v_full, st_filled, {}, now=None)[0]}
check("unfilled in-zone tiers still alert (control)", len(codes_alerted) >= 1, str(codes_alerted))


# ════════════════════════════════════════════════════════════════════
# L3-5 alert_state missing → fail-toward-alert: alert fires (never silently suppressed)
# ════════════════════════════════════════════════════════════════════
print("▶ t06_fail_toward_alert: missing/corrupt alert_state → alert fires once (fail-toward-alert)")
v_zone = _get_view(spot=57_000.0, manual_green=60_000.0)  # B3 in zone, not filled
# Empty alert_state (as if file was missing)
ta_empty, ns_empty = TA.alert_decisions(v_zone, _state(), {}, now=None)
check("empty alert_state → B3 still alerts", any(a["code"] == "B3" for a in ta_empty), str([a["code"] for a in ta_empty]))
# Corrupt (non-dict) → treated as empty
ta_corrupt, _ = TA.alert_decisions(v_zone, _state(), "garbage-not-a-dict", now=None)
check("corrupt alert_state (non-dict) → B3 still alerts", any(a["code"] == "B3" for a in ta_corrupt))
# Missing 'alerts' key → treated as empty
ta_noalerts, _ = TA.alert_decisions(v_zone, _state(), {"other": 1}, now=None)
check("missing 'alerts' key → B3 still alerts", any(a["code"] == "B3" for a in ta_noalerts))

print("▶ t07_load_alert_state_failopen: load_alert_state returns {} on missing/corrupt file")
with tempfile.TemporaryDirectory() as d:
    miss = pathlib.Path(d) / "nope.json"
    check("missing file → {}", TA.load_alert_state(miss) == {})
    bad = pathlib.Path(d) / "bad.json"
    bad.write_text("{half json", encoding="utf-8")
    check("corrupt JSON → {}", TA.load_alert_state(bad) == {})


# ════════════════════════════════════════════════════════════════════
# Dedup: same tier same day does not repeat
# ════════════════════════════════════════════════════════════════════
print("▶ t08_dedup_same_day: same tier same day does not alert twice")
from datetime import datetime  # noqa: E402
now1 = datetime.fromisoformat("2026-06-09T10:00:00+10:00")
v_zone2 = _get_view(spot=57_000.0, manual_green=60_000.0, now=now1)
# First run: B3 alerts, state records today
ta1, ns1 = TA.alert_decisions(v_zone2, _state(), {}, now=now1)
check("first run: B3 alerts", any(a["code"] == "B3" for a in ta1))
check("new_alert_state.alerts[B3] = 2026-06-09", ns1["alerts"]["B3"] == "2026-06-09", str(ns1))
# Second run same day with state from first run → no repeat
ta2, ns2 = TA.alert_decisions(v_zone2, _state(), ns1, now=now1)
check("same-day second run → B3 not alerted again", all(a["code"] != "B3" for a in ta2), str([a["code"] for a in ta2]))
check("state still shows B3=2026-06-09", ns2["alerts"]["B3"] == "2026-06-09")
# Next day → can alert again
now2 = datetime.fromisoformat("2026-06-10T10:00:00+10:00")
ta3, ns3 = TA.alert_decisions(v_zone2, _state(), ns1, now=now2)
check("next day → B3 can alert again", any(a["code"] == "B3" for a in ta3))
check("next-day state updated: B3=2026-06-10", ns3["alerts"]["B3"] == "2026-06-10")
check("dedup does not accidentally clear other tier history keys", "B3" in ns3["alerts"])


# ════════════════════════════════════════════════════════════════════
# Spot sanity: invalid/out-of-range spot → trigger prices shown but no alerts
# ════════════════════════════════════════════════════════════════════
print("▶ t09_spot_guard: invalid spot (None/out-of-range) → trigger prices shown but no alerts")
v_nospot = _get_view(spot=None, manual_green=60_000.0)
b3_ns = _tier(v_nospot, "B3")
check("no spot → B3 trigger prices still shown", b3_ns["triggers"] == [60_000.0, 58_000.0, 56_000.0])
check("no spot → dist_pct None", b3_ns["dist_pct"] is None)
check("no spot → alertable False", b3_ns["alertable"] is False)
check("no spot → spot echoed as None", v_nospot["spot"] is None)
to_alert_ns, _ = TA.alert_decisions(v_nospot, _state(), {}, now=None)
check("no spot → to_alert is empty", to_alert_ns == [], str(to_alert_ns))
# Out-of-range spot ($5) → treated as invalid
v_bad_spot = _get_view(spot=5.0, manual_green=60_000.0)
check("out-of-range spot → echoed as None", v_bad_spot["spot"] is None)
check("out-of-range spot → no alerts", TA.alert_decisions(v_bad_spot, _state(), {}, now=None)[0] == [])


# ════════════════════════════════════════════════════════════════════
# Alert message provenance: anchor value + source + fetched_at
# ════════════════════════════════════════════════════════════════════
print("▶ t10_message_provenance: alert message includes anchor provenance")
ta_msg, _ = TA.alert_decisions(_get_view(spot=57_000.0, manual_green=60_000.0), _state(), {}, now=None)
b3_msg = next(a for a in ta_msg if a["code"] == "B3")["message"]
check("message contains tier name", "B3 Green Line" in b3_msg, b3_msg)
check("message contains anchor fetched_at", "2026-06-09T08:00:00+10:00" in b3_msg, b3_msg)
check("message contains manual-review notice (does not execute automatically)", "record_fill" in b3_msg and "does not execute automatically" in b3_msg)


# ════════════════════════════════════════════════════════════════════
# Isolation: no state.json write, no record_fill call (three-layer isolation)
# ════════════════════════════════════════════════════════════════════
print("▶ t11_isolation_no_state_write: no state.json written during execution")
_here = pathlib.Path(__file__).resolve().parent
_before = set(glob.glob(str(_here / "**" / "*state*.json"), recursive=True))
for _ in range(3):
    vv = _get_view(spot=57_000.0, manual_green=60_000.0)
    TA.alert_decisions(vv, _state(), {}, now=None)
    TA.alert_decisions(vv, _state(fills_by_code={"B3": [{"price": 58000, "at": "2026-06-08T10:00:00+10:00"}]}), {}, now=None)
_after = set(glob.glob(str(_here / "**" / "*state*.json"), recursive=True))
check("build_tier_view/alert_decisions do not create state.json", _after == _before, f"new={_after - _before}")
check("no state.json in cwd",
      not os.path.exists(os.path.join(os.getcwd(), "state.json")) or
      os.path.join(os.getcwd(), "state.json") in _before)

print("▶ t12_isolation_no_record_fill: module does not import record_fill or exchange SDKs")
import ast  # noqa: E402
import inspect  # noqa: E402
src = inspect.getsource(TA)
# Static gate: AST parse confirms no import of record_fill and no calls to it
tree = ast.parse(src)
imported = set()
called = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        imported.update(n.name for n in node.names)
    elif isinstance(node, ast.ImportFrom):
        imported.add(node.module or "")
    elif isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            called.add(f.id)
        elif isinstance(f, ast.Attribute):
            called.add(f.attr)
check("no import of record_fill or exchange libraries", not (imported & {"record_fill", "binance", "ccxt", "exchange"}), str(imported))
check("no record_fill or order-placement function calls",
      not (called & {"record_fill", "create_order", "place_order", "submit_order"}), str(called & {"record_fill", "create_order", "place_order", "submit_order"}))
# Source-level forbidden symbol check
for forbidden in ("ccxt", "create_order", "place_order", "exchange.create", "client.order"):
    check(f"source contains no exchange/order symbol: {forbidden!r}", forbidden not in src, "found")

print("▶ t13_save_writes_only_alert_state: save_alert_state only writes alert_state.json, never state.json")
with tempfile.TemporaryDirectory() as d:
    ap = pathlib.Path(d) / "alert_state.json"
    _, ns = TA.alert_decisions(_get_view(spot=57_000.0, manual_green=60_000.0), _state(), {}, now=None)
    TA.save_alert_state(ns, ap)
    check("alert_state persisted and can be reloaded", TA.load_alert_state(ap) == ns)
    check("file is named alert_state.json (not state.json)", ap.exists() and ap.name == "alert_state.json")
    check("no state.json written to the directory", not (pathlib.Path(d) / "state.json").exists())
    check("no leftover .tmp file", not (pathlib.Path(d) / "alert_state.tmp").exists())


# ════════════════════════════════════════════════════════════════════
# B1/B2 = already-filled tiers (code invariant): never alert regardless of state.fills
# ════════════════════════════════════════════════════════════════════
print("▶ t14_b1b2_filled_invariant: B1/B2 already-filled tiers never alert (code invariant, independent of state.fills)")
# spot 67K: inside B1 zone (top 70K). B1 has no fills in state. Should still never alert.
v_b = _get_view(spot=67_000.0, state=_state(), manual_green=60_000.0)
b1 = _tier(v_b, "B1")
b2 = _tier(v_b, "B2")
check("B1 triggers are the fixed values [70000,68000,66000]", b1["triggers"] == [70_000.0, 68_000.0, 66_000.0], str(b1["triggers"]))
check("B1 spot 67K is in zone (in_zone=True)", b1["in_zone"] is True)
check("B1 filled=True (code invariant — independent of state.fills)", b1["filled"] is True)
check("B1 alertable=False (filled tiers never alert)", b1["alertable"] is False)
check("B2 filled=True (code invariant)", b2["filled"] is True)
check("B2 alertable=False", b2["alertable"] is False)
to_b, _ = TA.alert_decisions(v_b, _state(), {}, now=None)
check("B1 with no fills and in zone → still not in to_alert", all(a["code"] != "B1" for a in to_b), str([a["code"] for a in to_b]))
check("B2 with no fills and in zone → still not in to_alert", all(a["code"] != "B2" for a in to_b))
# Regression: B3 with fills in state → also not alerted (preserves state.fills logic)
st_b3f = _state(fills_by_code={"B3": [{"price": 58_000, "at": "2026-06-08T10:00:00+10:00"}]})
v_b3f = TA.build_tier_view(_cache(), st_b3f, manual_green=60_000.0, spot=57_000.0)
check("B3 with fills → filled=True (state.fills logic preserved)", _tier(v_b3f, "B3")["filled"] is True)
to_b3f, _ = TA.alert_decisions(v_b3f, st_b3f, {}, now=None)
check("B3 with fills → not in to_alert (regression guard)", all(a["code"] != "B3" for a in to_b3f))


# ════════════════════════════════════════════════════════════════════
# Stale green line: expired anchor → B3 fail-closed, not alerted
# ════════════════════════════════════════════════════════════════════
print("▶ t15_manual_green_staleness: stale green line → B3 fail-closed (no alert on expired anchor)")
import json as _json  # noqa: E402
from datetime import datetime as _dt, timedelta as _td  # noqa: E402
now_g = _dt.fromisoformat("2026-06-09T10:00:00+10:00")


def _anchors_file(d: pathlib.Path, content) -> pathlib.Path:
    """Write manual_anchors.json fixture to /tmp (config file, independent from ledger).
    content=None → do not write file (simulates missing config, B3 fail-closed)."""
    p = d / "manual_anchors.json"
    if content is not None:
        p.write_text(_json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return p


with tempfile.TemporaryDirectory() as _d:
    _dp = pathlib.Path(_d)
    # Fresh green line (updated_at = today) → accepted, not stale
    fresh_dir = _dp / "fresh"; fresh_dir.mkdir()
    fresh_p = _anchors_file(fresh_dir, {"green_line": {"value": 60_000.0, "updated_at": "2026-06-09"}})
    val_f, stale_f = TA.manual_green(now=now_g, path=fresh_p)
    check("fresh green line → value accepted as 60000", val_f == 60_000.0, str(val_f))
    check("fresh green line → not stale", stale_f is False)
    # Stale green line (updated_at 30 days ago, beyond 14-day threshold) → None + stale
    stale_dir = _dp / "stale"; stale_dir.mkdir()
    stale_p = _anchors_file(stale_dir, {"green_line": {"value": 60_000.0, "updated_at": "2026-05-09"}})
    val_s, stale_s = TA.manual_green(now=now_g, path=stale_p)
    check("stale green line (30 days ago) → value is None", val_s is None, str(val_s))
    check("stale green line → is_stale True", stale_s is True)
    # Missing file → value None, not stale (B3 fail-closed, no exception)
    miss_p = _dp / "missing" / "manual_anchors.json"
    val_b, stale_b = TA.manual_green(now=now_g, path=miss_p)
    check("missing file → value None, not stale", val_b is None and stale_b is False)
    # Corrupt file → value None, not stale (B3 fail-closed, no exception)
    bad_dir = _dp / "bad"; bad_dir.mkdir()
    bad_p = bad_dir / "manual_anchors.json"
    bad_p.write_text("{half json", encoding="utf-8")
    val_bad, stale_bad = TA.manual_green(now=now_g, path=bad_p)
    check("corrupt file → value None, not stale (fail-closed)", val_bad is None and stale_bad is False)
    # Bare numeric value (no updated_at) → backward-compatible, accepted as-is, not stale
    bare_dir = _dp / "bare"; bare_dir.mkdir()
    bare_p = _anchors_file(bare_dir, {"green_line": 58_000.0})
    val_bare, stale_bare = TA.manual_green(now=now_g, path=bare_p)
    check("bare numeric green line → accepted (backward-compatible), not stale", val_bare == 58_000.0 and stale_bare is False)
# Stale green line passed to build_tier_view (caller passes manual_green=None, green_stale=True) → B3 degraded
v_stale = TA.build_tier_view(_cache(), _state(), manual_green=None, spot=57_000.0, green_stale=True)
b3_stale = _tier(v_stale, "B3")
check("stale green line → B3 triggers None", b3_stale["triggers"] is None)
check("stale green line → B3 display = green line stale message", b3_stale["display"] == TA.DISPLAY_GREEN_STALE, b3_stale["display"])
check("stale green line → B3 alertable=False", b3_stale["alertable"] is False)
to_stale, _ = TA.alert_decisions(v_stale, _state(), {}, now=now_g)
check("stale green line → B3 not in to_alert (no alert on stale anchor)", all(a["code"] != "B3" for a in to_stale))


# ════════════════════════════════════════════════════════════════════
# Multiple tiers in zone simultaneously → all fire in the same round
# ════════════════════════════════════════════════════════════════════
print("▶ t16_multi_tier_same_day: spot in multiple zones → all fire together (expected behaviour)")
# green=60K → B3=[60000,58000,56000]; wma200=61K → C1=[61000,59000,57000];
# realized=53.6K → C2=[53600,51600,49600]. spot 57K:
#   B3 top 60K ✓ in zone · C1 top 61K ✓ in zone · C2 top 53.6K ✗ not in zone.
# B1/B2 are already-filled (code invariant) → never alert. Expected to_alert = {B3, C1}.
v_multi = _get_view(spot=57_000.0, manual_green=60_000.0)
to_multi, _ = TA.alert_decisions(v_multi, _state(), {}, now=None)
codes_multi = {a["code"] for a in to_multi}
check("spot 57K → B3 and C1 both fire in the same round", {"B3", "C1"} <= codes_multi, str(codes_multi))
check("already-filled B1/B2 not in the fired set (code invariant)", not ({"B1", "B2"} & codes_multi), str(codes_multi))
check("C2 (top 53.6K, spot not in zone) does not fire", "C2" not in codes_multi, str(codes_multi))


print(f"\n{'=' * 44}\nPassed {PASS} · Failed {FAIL}")
import sys  # noqa: E402
sys.exit(1 if FAIL else 0)
