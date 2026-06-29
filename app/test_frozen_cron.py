"""
test_frozen_cron.py — Trigger price freeze/drift cron runner unit tests (standalone, exit-0 style).

Coverage (config != ledger — three-layer isolation — E2/E3/E5 / three-tier guard / BTC+MSTR symmetry):
  FC-1  First run: no frozen snapshot → all open legs frozen + "updated" changes (reason=new),
        frozen_tiers.json written.
  FC-2  Drift < threshold on re-run → no new alert, only drift updated (frozen price unchanged).
  FC-3  Drift ≥ threshold (anchor drift) → frozen price advanced + alert message with old/new.
  FC-4  Fail-closed tier (green line missing) → B3 not frozen/not alerted (no false alarm);
        C1/C2 still frozen (degradation is not contagious).
  FC-5  --dry: decisions printed but frozen_tiers.json NOT written.
  FC-6  Three-layer isolation: state.json bytes unchanged after run (ledger is read-only);
        only frozen_tiers.json is written.
  FC-7  Filled legs are excluded from freezing (only open legs are tracked).
  FC-8  MSTR symmetry: mNAV legs frozen using real-time NAV per share + 3% threshold.
  FC-9  Isolation static gate: source contains no record_fill / order-placement / save_state.

Run: PYTHONPATH=app python3 app/test_frozen_cron.py (all green → exit 0).
Uses dict fixtures + monkeypatching throughout; no network, no real ledger;
state/cache/frozen all written to /tmp.
"""

import json
import os
import pathlib
import sys
import tempfile

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def _cache(wma200=61_000.0, realized=53_600.0):
    data = {"wma200": wma200, "realized_price": realized, "green_line": None, "green_line_source": "manual"}
    return {"brk": {"ok": True, "fetched_at": "2026-06-09T08:00:00+10:00", "data": data}}


def _btc_state(filled=None):
    """BTC ledger (new schema): B3/C1/C2 open legs with label+status.
    filled=set of labels → mark those legs as filled."""
    filled = filled or set()

    def leg(lab, price):
        return {"label": lab, "price": price, "rmb": 16000, "status": "filled" if lab in filled else "open"}
    return {"invested_rmb": 50000, "held_btc": 0.1, "tiers": [
        {"name": "B3 Green Line", "lo": 55000, "hi": 60000, "status": "open",
         "plan": [leg("B3-a", 59000), leg("B3-b", 57000), leg("B3-c", 55000)], "fills": []},
        {"name": "C1 Deep Dip", "lo": 52000, "hi": 55000, "status": "wait",
         "plan": [leg("C1-a", 54000), leg("C1-b", 52000)], "fills": []},
        {"name": "C2 Deep Dip", "lo": 48000, "hi": 52000, "status": "wait",
         "plan": [leg("C2-a", 51000), leg("C2-b", 48000)], "fills": []},
    ]}


def _mstr_state():
    return {"invested_usd": 0, "held_shares": 0, "rounds": [
        {"id": "T2", "name": "T2 Green Line Core", "layer": "Core", "status": "open", "plan": [
            {"label": "T2-a", "mnav": 0.78, "price": 101, "shares": 27, "status": "open"},
            {"label": "T2-b", "mnav": 0.70, "price": 91, "shares": 30, "status": "open"}], "fills": []},
    ]}


def _write(d, btc=None, mstr=None, cache=None, green=60_000.0):
    sp = d / "state.json"
    mp = d / "mstr.json"
    cp = d / "onchain_cache.json"
    fp = d / "frozen_tiers.json"
    ap = d / "manual_anchors.json"
    sp.write_text(json.dumps(btc if btc is not None else _btc_state(), ensure_ascii=False), "utf-8")
    mp.write_text(json.dumps(mstr if mstr is not None else _mstr_state(), ensure_ascii=False), "utf-8")
    cp.write_text(json.dumps(cache if cache is not None else _cache(), ensure_ascii=False), "utf-8")
    if green is not None:
        ap.write_text(json.dumps({"green_line": green}, ensure_ascii=False), "utf-8")
    os.environ["BTC_STATE"] = str(sp)
    os.environ["MSTR_STATE"] = str(mp)
    os.environ["ONCHAIN_CACHE"] = str(cp)
    os.environ["FROZEN_TIERS"] = str(fp)
    os.environ["MANUAL_ANCHORS"] = str(ap)
    return sp, mp, cp, fp


def _fresh():
    for m in ("frozen_cron", "frozen_tiers", "tier_alert", "onchain_signals", "mnav"):
        sys.modules.pop(m, None)
    import importlib
    import frozen_cron as C
    C = importlib.reload(C)
    # Stub out mNAV to avoid real network calls
    import mnav
    mnav.compute = lambda **kw: {"mstr": 120.0, "btc": 64000, "nav_per_share": 138.4, "mnav": 0.87}
    return C


print("▶ FC-1 first run: no frozen snapshot → all open legs frozen + file written")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, _, fp = _write(dd)
    C = _fresh()
    res = C.run(dry=False, weekly=False)
    check("first run produces BTC B3/C1/C2 changes", any(a.startswith("btc:B3") for a in res["alerted"]), res)
    check("frozen_tiers.json written", fp.exists())
    fz = json.loads(fp.read_text("utf-8"))
    check("frozen snapshot contains btc segment with B3-a", "B3-a" in fz["btc"]["legs"], fz)
    check("B3-a frozen price = 60000 (green line 60000 → B3-a first trigger = 60000)", fz["btc"]["legs"]["B3-a"]["price"] == 60000, fz["btc"]["legs"])


print("▶ FC-2 drift < threshold on re-run → no new alert, frozen price unchanged")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, _, fp = _write(dd, green=60_000.0)
    C = _fresh()
    C.run(dry=False, weekly=False)              # First run: freeze B3-a=60000
    before = json.loads(fp.read_text("utf-8"))["btc"]["legs"]["B3-a"]["price"]
    # Green line micro-drifts to 60300 (B3-a→60300, drift 0.5% < 2% and $300 < $1000) → under threshold
    _write(dd, green=60_300.0)
    C = _fresh()
    res = C.run(dry=False, weekly=False)
    after = json.loads(fp.read_text("utf-8"))["btc"]["legs"]["B3-a"]["price"]
    check("micro-drift → no btc:B3-a alert", not any(a == "btc:B3-a" for a in res["alerted"]), res)
    check("micro-drift → frozen price unchanged", after == before == 60000, (before, after))


print("▶ FC-3 drift ≥ threshold → frozen price advanced + alert")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, _, fp = _write(dd, green=60_000.0)
    C = _fresh()
    C.run(dry=False, weekly=False)
    # Green line drifts to 63000 (B3-a→63000, drift 5% ≥ 2%) → exceeds threshold
    _write(dd, green=63_000.0)
    C = _fresh()
    res = C.run(dry=False, weekly=False)
    check("threshold exceeded → btc:B3-a alert fired", "btc:B3-a" in res["alerted"], res)
    after = json.loads(fp.read_text("utf-8"))["btc"]["legs"]["B3-a"]["price"]
    check("threshold exceeded → frozen price advanced to 63000", after == 63000, after)


print("▶ FC-4 fail-closed (green line missing) → B3 not frozen/not alerted; C1/C2 still frozen (not contagious)")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, _, fp = _write(dd, green=None)  # No green line config → B3 fail-closed
    C = _fresh()
    res = C.run(dry=False, weekly=False)
    fz = json.loads(fp.read_text("utf-8"))
    check("B3 fail-closed → B3-a not in frozen legs", "B3-a" not in fz["btc"]["legs"], fz["btc"]["legs"])
    check("degradation not contagious → C1-a still frozen", "C1-a" in fz["btc"]["legs"], fz["btc"]["legs"])
    check("B3 no false alert", not any(a.startswith("btc:B3") for a in res["alerted"]), res)


print("▶ FC-5 --dry: decisions printed but frozen_tiers.json NOT written")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, _, fp = _write(dd)
    C = _fresh()
    res = C.run(dry=True, weekly=False)
    check("--dry sets dry=True in result", res["dry"] is True)
    check("--dry does NOT write frozen_tiers.json", not fp.exists(), f"exists={fp.exists()}")


print("▶ FC-6 three-layer isolation: state.json bytes unchanged (ledger is read-only); only frozen written")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    sp, mp, _, fp = _write(dd)
    sb, mb = sp.read_bytes(), mp.read_bytes()
    C = _fresh()
    C.run(dry=False, weekly=False)
    check("state.json bytes identical before and after run", sp.read_bytes() == sb)
    check("mstr.json bytes identical before and after run", mp.read_bytes() == mb)
    check("frozen_tiers.json was written", fp.exists())


print("▶ FC-7 filled legs excluded from freezing (only open legs are tracked)")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, _, fp = _write(dd, btc=_btc_state(filled={"B3-a"}))
    C = _fresh()
    C.run(dry=False, weekly=False)
    fz = json.loads(fp.read_text("utf-8"))
    check("filled B3-a not in frozen legs", "B3-a" not in fz["btc"]["legs"], fz["btc"]["legs"])
    check("still-open B3-b is in frozen legs", "B3-b" in fz["btc"]["legs"], fz["btc"]["legs"])


print("▶ FC-8 MSTR symmetry: mNAV legs frozen using real-time NAV per share + 3% threshold")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, _, fp = _write(dd)
    C = _fresh()
    res = C.run(dry=False, weekly=False)
    fz = json.loads(fp.read_text("utf-8"))
    # nps=138.4 → T2-a = round(0.78 × 138.4) = 108
    check("MSTR T2-a frozen price = round(0.78 × 138.4) = 108", fz["mstr"]["legs"]["T2-a"]["price"] == 108, fz.get("mstr"))
    check("first MSTR run produces change alerts", any(a.startswith("mstr:T2") for a in res["alerted"]), res)


print("▶ FC-9 isolation static gate: source contains no record_fill / order-placement / save_state")
C = _fresh()
import ast  # noqa: E402
import inspect  # noqa: E402
src = inspect.getsource(C)
tree = ast.parse(src)
called, imported = set(), set()
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
check("no import of record_fill or exchange libraries", not (imported & {"record_fill", "binance", "ccxt"}), str(imported))
check("no record_fill or order-placement function calls", not (called & {"record_fill", "create_order", "place_order"}), str(called))
for forbidden in ("create_order", "place_order", "save_state", "_save_cache"):
    check(f"source contains no dangerous symbol: {forbidden!r}", forbidden not in src, "found")


print("▶ FC-10 temporarily fail-closed tier → keeps old frozen price + no alert; recovery does not falsely report new entry (regression)")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    # Step 1: first run with green line → B3 all legs frozen (B3-a/b/c), C1/C2 also frozen
    _, _, _, fp = _write(dd, green=60_000.0)
    C = _fresh()
    C.run(dry=False, weekly=False)
    fz0 = json.loads(fp.read_text("utf-8"))
    b3a_before = fz0["btc"]["legs"]["B3-a"]["price"]
    at_before = fz0["btc"]["frozen_at"]
    check("step 1: B3-a frozen after first run", "B3-a" in fz0["btc"]["legs"], fz0["btc"]["legs"])
    # Step 2: green line missing → B3 entire tier fail-closed (B3 legs absent from live, not filled → lost-anchor)
    _write(dd, green=None)
    C = _fresh()
    res2 = C.run(dry=False, weekly=False)
    fz1 = json.loads(fp.read_text("utf-8"))
    check("lost-anchor: B3-a old frozen price preserved (not removed)", "B3-a" in fz1["btc"]["legs"], fz1["btc"]["legs"])
    check("lost-anchor: B3-a frozen price unchanged", fz1["btc"]["legs"]["B3-a"]["price"] == b3a_before, fz1["btc"]["legs"])
    check("lost-anchor: frozen_at unchanged", fz1["btc"]["frozen_at"] == at_before, fz1["btc"])
    check("lost-anchor: no B3 alert (no false alarm)", not any(a.startswith("btc:B3") for a in res2["alerted"]), res2)
    # Step 3: green line restored (same price) → B3-a already in prev → not "new", not over threshold → no false alert
    _write(dd, green=60_000.0)
    C = _fresh()
    res3 = C.run(dry=False, weekly=False)
    check("recovery: B3-a does not falsely report 'new entry'", not any(a == "btc:B3-a" for a in res3["alerted"]), res3)
    fz2 = json.loads(fp.read_text("utf-8"))
    check("recovery: B3-a frozen price is still the original baseline", fz2["btc"]["legs"]["B3-a"]["price"] == b3a_before, fz2["btc"]["legs"])


print("▶ FC-11 lost-anchor and filled coexist: filled legs still correctly removed (regression guard)")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    # First run: freeze everything
    _, _, _, fp = _write(dd, green=60_000.0)
    C = _fresh()
    C.run(dry=False, weekly=False)
    # B3-a filled + green line missing → B3 entire tier fail-closed (B3-b/c lost-anchor)
    _write(dd, btc=_btc_state(filled={"B3-a"}), green=None)
    C = _fresh()
    C.run(dry=False, weekly=False)
    fz = json.loads(fp.read_text("utf-8"))
    check("filled B3-a removed from frozen", "B3-a" not in fz["btc"]["legs"], fz["btc"]["legs"])
    check("lost-anchor B3-b preserved (not removed)", "B3-b" in fz["btc"]["legs"], fz["btc"]["legs"])


for k in ("BTC_STATE", "MSTR_STATE", "ONCHAIN_CACHE", "FROZEN_TIERS", "MANUAL_ANCHORS"):
    os.environ.pop(k, None)

print(f"\n{'=' * 44}\nPassed {PASS} · Failed {FAIL}")
sys.exit(1 if FAIL else 0)
