"""
test_dca_advice.py — test suite for dca_advice.py (standalone, no third-party deps).

Two coverage paths:
  1. Pure function advise(st, spot, rate): all tiers / downgrade / pause / price-fetch failure (controllable, no network, no disk).
  2. CLI subprocess (= the actual cron / skill call): --spot injection, the final JSON line, graceful errors on a missing/corrupted ledger.

Run: python3 test_dca_advice.py (needs PYTHONPATH=app to resolve strategy; run-tests.sh sets it). All green -> exit 0.

Note: the DCA tier labels asserted below (Pause/Cautious/Heavier/Double/Enhanced) and the tier name "B2 core" in the fixture
come from the strategy module's TIER_LABEL and the state.json schema; they are the verbatim English values the system emits.
"""

import copy
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import dca_advice  # noqa: E402

SCRIPT = str(pathlib.Path(__file__).parent / "dca_advice.py")
RATE = 7.0

# No fills -> dip_filled_last_7d=False (no downgrade)
BASE = {
    "updated": "2026-06-03T00:00:00+10:00",
    "held_btc": 0.1, "invested_rmb": 50000, "avg_cost_usd": 68000,
    "budget_total_rmb": 200000, "rate_rmb_per_usdt": RATE,
    "tracks": [
        {"name": "Total budget used", "used": 50000, "quota": 200000, "color": "green"},
        {"name": "DCA", "used": 3000, "quota": 30000, "color": "blue"},
        {"name": "B core dip-buy", "used": 0, "quota": 88000, "color": "amber"},
        {"name": "C deep-dip reserve", "used": 0, "quota": 30135, "color": "red"},
    ],
    "tiers": [
        {"name": "B2 core", "lo": 60000, "hi": 66000, "amount": 28000, "status": "open", "sem": "amber",
         "plan": [{"price": 64000, "rmb": 14000}], "fills": []},
    ],
    "onchain": [],
}

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def state(invested=50000, dip=False):
    """Build a ledger; when dip=True insert a 'just now' dip fill (triggers rule ② downgrade), without depending on a fixed date."""
    s = copy.deepcopy(BASE)
    s["invested_rmb"] = invested
    s["tracks"][0]["used"] = invested
    if dip:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        s["tiers"][0]["fills"] = [{"price": 64000, "rmb": 14000, "usd": 2000, "btc": 0.03, "at": now}]
    return s


def fixture(tmp, **kw):
    p = pathlib.Path(tmp) / "state.json"
    p.write_text(json.dumps(state(**kw), ensure_ascii=False), "utf-8")
    return p


def run_cli_env(state_path, base_env, *args):
    """Run the CLI with a custom env (used to inject GOLDEN_WINDOW_END for window pacing)."""
    env = {**base_env, "BTC_STATE": str(state_path)}
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        res = {"_raw": r.stdout, "_err": r.stderr}
    return res, r.stdout


def run_cli(state_path, *args):
    return run_cli_env(state_path, dict(os.environ), *args)


# ── 1. Legacy fixed-tier regression (advise has switched to window mode; the legacy assertions test decide_weekly(window_end=None) directly) ──
# advise() runs window pacing in production, but the legacy fixed-tier logic must stay backward-compatible -> test strategy.decide_weekly directly.
import strategy  # noqa: E402  PYTHONPATH=app (run-tests.sh sets it)


def legacy(spot, invested=50000, dip=False):
    """Direct legacy-logic test: window_end=None -> fixed tiers (backward-compat regression)."""
    return strategy.decide_weekly(strategy.WeeklyContext(
        btc_price_usd=spot, invested_rmb=invested, available_usdt=10 ** 9,
        dip_filled_last_7d=dip, rate_rmb_per_usdt=RATE, window_end=None))


print("▶ t01_tiers_no_dip: legacy fixed tiers (window_end=None, regression)")
for spot, amt, tier in [(75000, 3000, "Cautious"), (67000, 5500, "Heavier"),
                        (55000, 7500, "Double"), (49000, 10000, "Enhanced")]:
    d = legacy(spot)
    check(f"${spot} -> {tier} ¥{amt}", d.amount_rmb == amt
          and strategy.TIER_LABEL[d.final_tier] == tier and d.action == "BUY", str(d.amount_rmb))

print("▶ t02_pause_high: ≥$80K -> pause SKIP ¥0 (legacy logic)")
d = legacy(82000)
check("$82K pause", d.amount_rmb == 0 and d.action == "SKIP" and d.final_tier == strategy.Tier.PAUSE, str(d))

print("▶ t03_boundaries: boundary attribution (upper edge belongs to the next tier down, legacy logic)")
for spot, tier in [(70000, "Cautious"), (80000, "Pause"), (58000, "Heavier"), (50000, "Double")]:
    d = legacy(spot)
    check(f"${spot} boundary -> {tier}", strategy.TIER_LABEL[d.final_tier] == tier, str(d.final_tier))

print("▶ t04_downgrade: a dip in the last 7 days -> downgrade one tier (legacy logic)")
for spot, base, final, amt in [(67000, "Heavier", "Cautious", 3000), (55000, "Double", "Heavier", 5500),
                               (49000, "Enhanced", "Double", 7500)]:
    d = legacy(spot, dip=True)
    check(f"${spot} {base}->{final} ¥{amt}", strategy.TIER_LABEL[d.base_tier] == base
          and strategy.TIER_LABEL[d.final_tier] == final and d.amount_rmb == amt, str(d))

print("▶ t05_downgrade_floor: the Cautious tier is already the floor; even with a dip it does not go lower (legacy logic)")
d = legacy(75000, dip=True)
check("$75K Cautious no downgrade", strategy.TIER_LABEL[d.final_tier] == "Cautious" and d.amount_rmb == 3000
      and d.final_tier == d.base_tier, str(d))

print("▶ t06_pause_cap: cumulative ≥¥180K(90%) -> pause (legacy logic)")
d = legacy(55000, invested=185000)
check("≥180K pause", d.final_tier == strategy.Tier.PAUSE and d.action == "SKIP", str(d))

# ── 1b. advise() window-pacing new assertions (production convention) ──
print("▶ t01b_advise_window: advise() runs window pacing (remaining pool / remaining weeks * tilt)")
os.environ["GOLDEN_WINDOW_END"] = "2026-09-12"
_, r = dca_advice.advise(state(invested=126453), 63000, RATE)
check("Heavier tier window pacing BUY · amount>0 · not downgraded",
      r["ok"] and r["action"] == "BUY" and r["base_tier"] == "Heavier"
      and r["week_amount_rmb"] > 0 and not r["downgraded"], str(r))
# the pause tier still buys in window mode (tilt 0.5), no longer a whole-week SKIP
_, r = dca_advice.advise(state(invested=100000), 85000, RATE)
check("pause tier still BUYs in window mode (≠ legacy SKIP)", r["action"] == "BUY" and r["week_amount_rmb"] > 0, str(r))
del os.environ["GOLDEN_WINDOW_END"]

# ── 4. Price-fetch failure: no made-up price ──
print("▶ t07_fetch_fail: spot fetch fails -> no made-up price, degraded notice")
text, r = dca_advice.advise(state(), None, RATE)
check("ok=false no amount", (not r["ok"]) and r["week_amount_rmb"] is None, str(r))
check("text says 'fetch failed' with no price", "fetch failed" in text and "$" not in text, text)

# ── 5. JSON structure complete ──
print("▶ t08_json_shape: result fields complete")
_, r = dca_advice.advise(state(), 67000, RATE)
check("all fields present", all(k in r for k in ("ok", "week_amount_rmb", "tier", "base_tier",
      "downgraded", "btc_price", "action", "reasons")) and isinstance(r["reasons"], list), str(r))

# ── 6. CLI integration (subprocess = the actual call, window-pacing convention) ──
print("▶ t09_cli_spot: CLI --spot injection, last line is valid JSON")
with tempfile.TemporaryDirectory() as tmp:
    env_we = {**os.environ, "GOLDEN_WINDOW_END": "2026-09-12"}
    res, raw = run_cli_env(fixture(tmp), env_we, "--spot", "67000")
    check("CLI ok and window-pacing BUY amount>0", res.get("ok") and res.get("action") == "BUY"
          and res.get("base_tier") == "Heavier" and res.get("week_amount_rmb", 0) > 0, str(res))
    check("stdout has human text", "This week's DCA" in raw, raw[:80])

print("▶ t12_dip_7d_edges: dip_filled_last_7d single-source (strategy) boundaries")
import strategy  # noqa: E402  PYTHONPATH=app (run-tests.sh sets it)
_now = datetime.datetime.now().astimezone()
_mk = lambda days_ago, status="partial": {"tiers": [{"status": status, "fills": [
    {"price": 64000, "rmb": 14000, "at": (_now - datetime.timedelta(days=days_ago)).isoformat()}]}]}
check("filled 6 days ago -> True (a partial tier counts too, status no longer misjudges)", strategy.dip_filled_last_7d(_mk(6), now=_now) is True)
check("filled 8 days ago -> False (status=done doesn't count either, truly by 7 days)", strategy.dip_filled_last_7d(_mk(8, "done"), now=_now) is False)
check("no fill -> False", strategy.dip_filled_last_7d({"tiers": [{"status": "done", "fills": []}]}, now=_now) is False)
check("a backdated bare date parses (yesterday -> True)",
      strategy.dip_filled_last_7d({"tiers": [{"fills": [{"at": (_now - datetime.timedelta(days=1)).date().isoformat()}]}]}, now=_now) is True)
check("a corrupted at is skipped without crashing", strategy.dip_filled_last_7d({"tiers": [{"fills": [{"at": "garbage"}]}]}, now=_now) is False)

print("▶ t13_same_iso_week: this-week check (ISO Monday as the boundary)")
_mon = datetime.datetime(2026, 6, 1, 9, 0, tzinfo=_now.tzinfo)   # 2026-06-01 Monday
_fri = datetime.datetime(2026, 6, 5, 18, 0, tzinfo=_now.tzinfo)  # same Friday
_sun_prev = datetime.datetime(2026, 5, 31, 23, 0, tzinfo=_now.tzinfo)  # previous Sunday
check("Monday entry -> still this week on Friday", strategy.same_iso_week(_mon.isoformat(), now=_fri) is True)
check("previous-Sunday entry -> not this week on Monday", strategy.same_iso_week(_sun_prev.isoformat(), now=_mon) is False)
check("missing/corrupted -> False", strategy.same_iso_week("", now=_now) is False and strategy.same_iso_week("nope", now=_now) is False)

print("▶ t10_cli_missing: ledger does not exist -> graceful error")
res, _ = run_cli("/nonexistent/state.json", "--spot", "67000")
check("ok=false no raw raise", res.get("ok") is False and "msg" in res, str(res))

print("▶ t11_cli_corrupt: ledger corrupted -> graceful error")
with tempfile.TemporaryDirectory() as tmp:
    p = pathlib.Path(tmp) / "state.json"
    p.write_text("{ broken json", "utf-8")
    res, _ = run_cli(p, "--spot", "67000")
    check("corrupted ok=false", res.get("ok") is False and "msg" in res, str(res))

print(f"\n{'=' * 40}\nPassed {PASS} · Failed {FAIL}")
sys.exit(1 if FAIL else 0)
