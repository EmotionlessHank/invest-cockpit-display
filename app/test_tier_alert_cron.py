"""
test_tier_alert_cron.py — Dip tier price alert cron runner unit tests (standalone, exit-0 style).

Coverage (three-layer isolation):
  C-1 --dry: alerts print but alert_state is NOT written, no extra side effects
  C-2 --check: alerts write alert_state (independent file)
  C-3 spot fetch failure (get_spot→None, no --spot override) → no fabricated price, no false alert,
       no alert_state write
  C-4 spot not in any tier zone → no alerts, no alert_state write (idempotent)
  C-5 same-tier same-day dedup: --check run twice, second run does not re-alert
  C-6 filled tiers not alerted (reads state.json tiers[].fills, read-only)
  C-7 build_tier_view does not write back to ledger: state.json bytes unchanged after run
  C-8 no network calls (get_spot monkeypatched); --spot path skips get_spot entirely
  C-9 green line anchor via independent config file manual_anchors.json (config != ledger):
       missing/stale → B3 fail-closed (no alert); fresh → B3 anchored and alertable;
       app and cron both read through tier_alert.manual_green() (single source)

Run: PYTHONPATH=app python3 app/test_tier_alert_cron.py (all green → exit 0).
Uses dict fixtures + monkeypatching throughout; no network, no real ledger;
state/cache/alert_state all written to /tmp.
"""

import datetime
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


# ── Fixtures: cache / state (tier name prefixes aligned B1..C2; optional fills injection) ────
def _cache(wma200=61_000.0, realized=53_600.0):
    data = {"wma200": wma200, "realized_price": realized,
            "green_line": None, "green_line_source": "manual"}
    return {"brk": {"ok": True, "fetched_at": "2026-06-09T08:00:00+10:00", "data": data}}


def _state(fills_by_code=None):
    """state.json shape (ledger): green line has been moved out of the ledger to the separate
    config file manual_anchors.json (config != ledger)."""
    fills_by_code = fills_by_code or {}
    names = {"B1": "B1 Probe", "B2": "B2 Core", "B3": "B3 Green Line", "C1": "C1 Deep Dip", "C2": "C2 Deep Dip"}
    tiers = []
    for code, name in names.items():
        tiers.append({"name": name, "lo": 0, "hi": 0, "status": "open",
                      "plan": [], "fills": list(fills_by_code.get(code, []))})
    return {"tiers": tiers, "invested_rmb": 50_000}


def _write_fixtures(d: pathlib.Path, state: dict, cache: dict, green_line=60_000.0):
    """Write fixtures to /tmp and point environment variables at them
    (tests must never touch the real ledger/cache).
    Green line (B3 anchor) goes in the independent config file manual_anchors.json
    (MANUAL_ANCHORS env var):
      green_line=None → do not write the file (simulates missing config, B3 fail-closed);
      numeric → {"green_line": <num>};
      dict → written as-is (supports {value, updated_at} freshness format)."""
    sp = d / "state.json"
    cp = d / "onchain_cache.json"
    ap = d / "alert_state.json"
    mp = d / "manual_anchors.json"
    sp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    cp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    if green_line is not None:
        anchors = green_line if isinstance(green_line, dict) else {"green_line": green_line}
        mp.write_text(json.dumps(anchors, ensure_ascii=False), encoding="utf-8")
    os.environ["BTC_STATE"] = str(sp)
    os.environ["ONCHAIN_CACHE"] = str(cp)
    os.environ["ALERT_STATE"] = str(ap)
    os.environ["MANUAL_ANCHORS"] = str(mp)
    return sp, cp, ap


def _fresh_import():
    """Reload modules with current env vars (BTC_STATE / ONCHAIN_CACHE / ALERT_STATE /
    MANUAL_ANCHORS are bound at import time)."""
    for m in ("tier_alert_cron", "tier_alert", "onchain_signals"):
        sys.modules.pop(m, None)
    import importlib
    import tier_alert_cron as C  # noqa
    return importlib.reload(C)


def _run(cron, dry, spot_override=None, net_spot="__nonet__"):
    """Run once, monkeypatching get_spot to prevent any real network calls.
    net_spot=None simulates a fetch failure."""
    if net_spot != "__nonet__":
        cron.get_spot = lambda: net_spot
    else:
        cron.get_spot = lambda: (_ for _ in ()).throw(AssertionError("No network calls allowed: test must monkeypatch get_spot"))
    return cron.run(dry=dry, spot_override=spot_override)


# ════════════════════════════════════════════════════════════════════
print("▶ C-1 --dry: alerts print but alert_state is NOT written")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, ap = _write_fixtures(dd, _state(), _cache())
    C = _fresh_import()
    # spot 57K enters B3 zone (green=60K → B3=[60000,58000,56000])
    res = _run(C, dry=True, spot_override=57_000.0)
    check("--dry still reports B3 alerted", "B3" in res["alerted"], str(res))
    check("--dry sets dry=True in result", res.get("dry") is True)
    check("--dry does NOT write alert_state file", not ap.exists(), f"exists={ap.exists()}")


print("▶ C-2 --check: alerts write alert_state (independent file)")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    sp, cp, ap = _write_fixtures(dd, _state(), _cache())
    C = _fresh_import()
    res = _run(C, dry=False, spot_override=57_000.0)
    check("--check reports B3 alerted", "B3" in res["alerted"], str(res))
    check("--check writes alert_state.json", ap.exists())
    check("written file is named alert_state.json (not state.json)", ap.name == "alert_state.json")
    saved = json.loads(ap.read_text(encoding="utf-8"))
    check("alert_state records B3 with today's date", "B3" in saved.get("alerts", {}), str(saved))
    check("no leftover .tmp file", not (dd / "alert_state.tmp").exists())


print("▶ C-3 spot fetch failure (get_spot→None, no --spot) → no fabricated price, no false alert")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, ap = _write_fixtures(dd, _state(), _cache())
    C = _fresh_import()
    res = _run(C, dry=False, spot_override=None, net_spot=None)  # simulate fetch failure
    check("fetch failure → ok=False", res["ok"] is False, str(res))
    check("fetch failure → msg='price fetch failed'", res.get("msg") == "price fetch failed")
    check("fetch failure → no alerts (no false alert)", res["alerted"] == [])
    check("fetch failure → alert_state NOT written", not ap.exists())


print("▶ C-4 spot not in any tier zone → no alerts, alert_state not written (idempotent)")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, ap = _write_fixtures(dd, _state(), _cache())
    C = _fresh_import()
    # spot 90K: far above all tiers (B3 top 60K / C1 top 61K / C2 top ~53.6K)
    res = _run(C, dry=False, spot_override=90_000.0)
    check("not in zone → ok=True, no alerts", res["ok"] is True and res["alerted"] == [], str(res))
    check("no alerts → alert_state NOT written (idempotent)", not ap.exists())


print("▶ C-5 same-tier same-day dedup: second --check run does not re-alert")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _, _, ap = _write_fixtures(dd, _state(), _cache())
    C = _fresh_import()
    r1 = _run(C, dry=False, spot_override=57_000.0)
    check("first run: B3 alerted", "B3" in r1["alerted"])
    r2 = _run(C, dry=False, spot_override=57_000.0)  # same day, re-reads alert_state
    check("same-day second run: B3 NOT re-alerted", "B3" not in r2["alerted"], str(r2))


print("▶ C-6 filled tier not alerted (reads state.json tiers[].fills, read-only)")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    st = _state(fills_by_code={"B3": [{"price": 58_000, "rmb": 16000, "at": "2026-06-08T10:00:00+10:00"}]})
    _, _, ap = _write_fixtures(dd, st, _cache())
    C = _fresh_import()
    res = _run(C, dry=False, spot_override=57_000.0)
    check("B3 filled → not in alerted list", "B3" not in res["alerted"], str(res))


print("▶ C-7 build_tier_view does not write back to ledger: state.json bytes unchanged")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    sp, cp, ap = _write_fixtures(dd, _state(), _cache())
    before = sp.read_bytes()
    C = _fresh_import()
    _run(C, dry=False, spot_override=57_000.0)
    _run(C, dry=True, spot_override=57_000.0)
    after = sp.read_bytes()
    check("state.json bytes identical before and after run (read-only)", before == after)


print("▶ C-8 no network calls (get_spot monkeypatched; --spot path does not invoke get_spot)")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    _write_fixtures(dd, _state(), _cache())
    C = _fresh_import()
    # net_spot=__nonet__ → get_spot replaced with an AssertionError raiser;
    # --spot provided → run() must never reach get_spot
    try:
        res = _run(C, dry=True, spot_override=57_000.0, net_spot="__nonet__")
        check("--spot path never calls get_spot (no network)", "B3" in res["alerted"], str(res))
    except AssertionError as e:
        check("--spot path never calls get_spot (no network)", False, str(e))


print("▶ C-9 green line config file (manual_anchors.json): missing/stale → B3 fail-closed; fresh → alertable")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    # Missing config (green_line=None → file not written) → green line None → B3 unavailable, no alert
    _, _, _ap = _write_fixtures(dd, _state(), _cache(), green_line=None)
    check("missing config: manual_anchors.json not written", not (dd / "manual_anchors.json").exists())
    C = _fresh_import()
    res_no = _run(C, dry=True, spot_override=57_000.0)
    check("missing config → B3 not alerted (fail-closed)", "B3" not in res_no["alerted"], str(res_no))
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    # Fresh green line with {value, updated_at} format (today's date, avoids date rot) → B3 anchored, alertable
    _write_fixtures(dd, _state(), _cache(),
                    green_line={"green_line": {"value": 60_000.0,
                                               "updated_at": datetime.date.today().isoformat()}})
    C = _fresh_import()
    res_obj = _run(C, dry=True, spot_override=57_000.0)
    check("fresh {value, updated_at} green line → B3 alerted (anchor active)", "B3" in res_obj["alerted"], str(res_obj))
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    # Stale green line (updated_at far beyond the 14-day threshold) → B3 fail-closed/stale, no alert
    _write_fixtures(dd, _state(), _cache(),
                    green_line={"green_line": {"value": 60_000.0, "updated_at": "2000-01-01"}})
    C = _fresh_import()
    res_stale = _run(C, dry=True, spot_override=57_000.0)
    check("stale green line (updated_at > 14d) → B3 not alerted (fail-closed/stale)",
          "B3" not in res_stale["alerted"], str(res_stale))
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    # Single source: app (build_dashboard) and cron both read through tier_alert.manual_green()
    # from the same manual_anchors.json. Verify cron's green line matches a direct call.
    _write_fixtures(dd, _state(), _cache(), green_line=60_000.0)
    C = _fresh_import()
    g_direct, s_direct = C.tier_alert.manual_green()
    check("single source: manual_green() reads manual_anchors.json → 60000", g_direct == 60_000.0, str(g_direct))
    check("single source: fresh config → not stale", s_direct is False, str(s_direct))
    # cron run() uses the same tier_alert.manual_green() → same green line value → B3 alerted
    res_single = _run(C, dry=True, spot_override=57_000.0)
    check("single source: cron run() uses same green line → B3 alerted", "B3" in res_single["alerted"], str(res_single))


print("▶ C-10 isolation: no record_fill import, no state.json write")
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    sp, cp, ap = _write_fixtures(dd, _state(), _cache())
    C = _fresh_import()
    import ast
    import inspect
    src = inspect.getsource(C)
    tree = ast.parse(src)
    called = set()
    imported = set()
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
    check("no import of record_fill or exchange libraries",
          not (imported & {"record_fill", "binance", "ccxt", "exchange"}), str(imported))
    check("no record_fill or order-placement function calls",
          not (called & {"record_fill", "create_order", "place_order", "submit_order"}),
          str(called & {"record_fill", "create_order", "place_order", "submit_order"}))
    for forbidden in ("ccxt", "create_order", "place_order", "_save_cache", "save_state"):
        check(f"source contains no dangerous symbol: {forbidden!r}", forbidden not in src, "found")
    # After a production run the directory should contain exactly these four files
    _run(C, dry=False, spot_override=57_000.0)
    names = sorted(p.name for p in dd.iterdir())
    check("directory contains exactly the expected files after run (no unexpected disk writes)",
          names == ["alert_state.json", "manual_anchors.json", "onchain_cache.json", "state.json"], str(names))


# Clean up env vars so they don't leak into other test runs
for k in ("BTC_STATE", "ONCHAIN_CACHE", "ALERT_STATE", "MANUAL_ANCHORS"):
    os.environ.pop(k, None)

print(f"\n{'=' * 44}\nPassed {PASS} · Failed {FAIL}")
sys.exit(1 if FAIL else 0)
