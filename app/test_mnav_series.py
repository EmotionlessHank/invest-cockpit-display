"""
test_mnav_series.py — mNAV historical series calculation tests.
All data is injected (no network calls). Verifies: stepped-timeline lookup,
calibration convention (diluted shares / no net-debt), date alignment,
graceful degradation (no fabrication), and 2-decimal-place agreement with the
saylortracker navPremiumDiluted convention
(using their published 8-K data points + same-day prices).
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mnav  # noqa: E402

_fail = 0


def check(name, cond, ctx=""):
    global _fail
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"  {ctx}"))
    if not cond:
        _fail += 1


TL = [
    {"date": "2026-04-01", "btc_holdings": 762099, "diluted_shares_m": 377.847},
    {"date": "2026-04-20", "btc_holdings": 815061, "diluted_shares_m": 381.588},
    {"date": "2026-06-22", "btc_holdings": 847363, "diluted_shares_m": 388.617},
]


def t01_caliber_and_lookup():
    """Convention = diluted shares / no net debt; each date uses the most recent timeline entry ≤ that date."""
    mstr_daily = [("2026-04-01", 122.78), ("2026-04-21", 120.0), ("2026-06-29", 82.31)]
    btc_daily = {"2026-04-01": 68112.0, "2026-04-21": 67000.0, "2026-06-29": 59287.02}
    s = mnav.get_mnav_series(90, mstr_daily=mstr_daily, btc_daily=btc_daily, timeline=TL)
    check("returns 3 data points", len(s) == 3, str(s))
    # 04-01 uses the first timeline entry
    nps0 = 762099 * 68112.0 / (377.847e6)
    check("04-01: mNAV calibration correct", abs(s[0]["mnav"] - round(122.78 / nps0, 4)) < 1e-9, str(s[0]))
    # 04-21 matches the 04-20 stepped entry (most recent ≤ 04-21)
    nps1 = 815061 * 67000.0 / (381.588e6)
    check("04-21: matches 04-20 timeline step", abs(s[1]["mnav"] - round(120.0 / nps1, 4)) < 1e-9, str(s[1]))
    # 06-29 matches the 06-22 entry → 2dp agreement with saylortracker navPremiumDiluted: 0.64
    check("06-29: 2dp agreement with saylortracker", round(s[2]["mnav"], 2) == 0.64, str(s[2]))


def t02_window_trim():
    """window_days truncation returns only the last N trading days."""
    md = [(f"2026-06-{d:02d}", 80.0) for d in range(1, 21)]
    bd = {f"2026-06-{d:02d}": 60000.0 for d in range(1, 21)}
    s = mnav.get_mnav_series(5, mstr_daily=md, btc_daily=bd, timeline=TL)
    check("window=5 → 5 points", len(s) == 5, str(len(s)))
    check("tail-aligned to the last trading day", s[-1]["date"] == "2026-06-20", str(s[-1]))


def t03_missing_btc_day_skipped():
    """Missing BTC price for a day → that day is skipped; no fabrication."""
    md = [("2026-06-25", 82.0), ("2026-06-26", 82.5), ("2026-06-29", 82.31)]
    bd = {"2026-06-25": 59000.0, "2026-06-29": 59287.02}  # 06-26 missing
    s = mnav.get_mnav_series(90, mstr_daily=md, btc_daily=bd, timeline=TL)
    check("day with missing price is skipped (3→2)", len(s) == 2 and all(p["date"] != "2026-06-26" for p in s), str(s))


def t04_degrade_empty_on_source_fail():
    """Empty price source or empty timeline → returns [], no exception, no fabrication."""
    check("empty MSTR source → []", mnav.get_mnav_series(90, mstr_daily=[], btc_daily={"x": 1}, timeline=TL) == [])
    check("empty BTC source → []", mnav.get_mnav_series(90, mstr_daily=[("2026-06-29", 82.31)], btc_daily={}, timeline=TL) == [])
    check("empty timeline → []", mnav.get_mnav_series(90, mstr_daily=[("2026-06-29", 82.31)], btc_daily={"2026-06-29": 59287.0}, timeline=[]) == [])


def t05_real_timeline_loads():
    """The co-deployed mstr_holdings_timeline.json loads successfully and has the required fields."""
    tl = mnav.load_timeline()
    check("timeline non-empty", len(tl) > 10, str(len(tl)))
    check("entries have all required fields", all({"date", "btc_holdings", "diluted_shares_m"} <= set(x) for x in tl[:3]), str(tl[:1]))


for fn in (t01_caliber_and_lookup, t02_window_trim, t03_missing_btc_day_skipped,
           t04_degrade_empty_on_source_fail, t05_real_timeline_loads):
    print(f"▶ {fn.__name__}: {fn.__doc__.strip().splitlines()[0]}")
    fn()

print("✓ All passed" if _fail == 0 else f"✗ Failed {_fail}")
sys.exit(1 if _fail else 0)
