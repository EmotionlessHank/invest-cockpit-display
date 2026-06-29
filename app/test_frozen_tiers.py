"""
test_frozen_tiers.py — Trigger price freeze + drift detection unit tests (standalone, frozen_tiers pure/semi-pure functions).

Coverage (real-money scenario — E2 thresholds / E3 weekly reset / E5 merge / config != ledger):
  T-B2 drift < threshold → only drift field updated, price/frozen_at unchanged, no change entry.
  T-B3 drift ≥ threshold → frozen price advanced + frozen_at updated + change entry (old/new/drift%/anchor).
  T-B4 threshold boundary: BTC max(2%,$1000) dual-criteria three-point pin; MSTR 3% three-point pin.
  T-B5 fetch-failed/lost-anchor legs (not passed into live_legs) → keep old frozen, no drift, no false alert.
  T-B6 weekly reset (E3): force_weekly=True recomputes even under threshold + change(reason=weekly).
  T-B7 multi-leg merge alert (E5): build_update_message combines same-round legs into one message.
  T-B8 frozen != ledger: load/save only touch the frozen file, never state.json (path isolation + /tmp).

Run: PYTHONPATH=app python3 app/test_frozen_tiers.py (all green → exit 0).
"""

import pathlib
import tempfile
from datetime import datetime

import frozen_tiers as FT

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def _live(price, anchor="green_line", anchor_val=None):
    return {"price": price, "anchor": anchor, "anchor_val": anchor_val if anchor_val is not None else price}


# A weekday (non-Monday) to suppress automatic weekly reset in most tests
TUE = datetime.fromisoformat("2026-06-09T12:00:00+10:00")  # 2026-06-09 is a Tuesday
MON = datetime.fromisoformat("2026-06-08T12:00:00+10:00")  # 2026-06-08 is a Monday


print("▶ T-B-first initial freeze: no prev → all legs frozen + change(reason=new)")
snap, changes = FT.detect_drift("btc", {"B3-a": _live(60000)}, prev=None, now=TUE)
check("first freeze → B3-a frozen at 60000", snap["legs"]["B3-a"]["price"] == 60000, snap)
check("first freeze → change reason=new", changes and changes[0]["reason"] == "new", changes)
check("first freeze → frozen_at is set", snap.get("frozen_at"), snap)


print("▶ T-B2 drift < threshold → only drift updated, price/frozen_at unchanged, no change")
prev = {"frozen_at": "2026-06-01T00:00:00+10:00", "legs": {"B3-a": _live(60000)}, "drift": {}}
# live 60500: drift 0.83% < 2% and absolute diff $500 < $1000 → below threshold
snap2, ch2 = FT.detect_drift("btc", {"B3-a": _live(60500)}, prev=prev, now=TUE)
check("under threshold → no changes", ch2 == [], ch2)
check("under threshold → frozen price unchanged (still 60000)", snap2["legs"]["B3-a"]["price"] == 60000, snap2)
check("under threshold → frozen_at unchanged", snap2["frozen_at"] == prev["frozen_at"], snap2)
check("under threshold → drift updated ≈0.0083", abs(snap2["drift"]["B3-a"] - 0.0083) < 0.0005, snap2["drift"])


print("▶ T-B3 drift ≥ threshold → price advanced + change with old/new/drift%/anchor")
# live 62000: drift 3.33% ≥ 2% → exceeds threshold
snap3, ch3 = FT.detect_drift("btc", {"B3-a": _live(62000, anchor="green_line", anchor_val=62000)},
                             prev=prev, now=TUE)
check("exceeds threshold → frozen price advanced to 62000", snap3["legs"]["B3-a"]["price"] == 62000, snap3)
check("exceeds threshold → frozen_at updated (differs from prev)", snap3["frozen_at"] != prev["frozen_at"], snap3)
c = ch3[0]
check("change reason=threshold", c["reason"] == "threshold", c)
check("change carries old=60000 / new=62000", c["old"] == 60000 and c["new"] == 62000, c)
check("change carries drift% ≈0.0333", abs(c["drift_pct"] - 0.0333) < 0.0005, c)
check("change carries anchor source 'green_line'", c["anchor"] == "green_line", c)


print("▶ T-B4 BTC threshold boundary max(2%,$1000): percentage criterion three-point pin")
base = {"frozen_at": "x", "legs": {"L": _live(50000)}, "drift": {}}
# High-price baseline to isolate the percentage criterion: base 100000
hi = {"frozen_at": "x", "legs": {"L": _live(100000)}, "drift": {}}
_, c_eq = FT.detect_drift("btc", {"L": _live(102000)}, prev=hi, now=TUE)   # exactly 2.0%
check("exactly 2% → exceeds threshold (≥)", c_eq != [], c_eq)
_, c_just = FT.detect_drift("btc", {"L": _live(102001)}, prev=hi, now=TUE)  # just over 2%
check("just over 2% → exceeds threshold", c_just != [], c_just)
_, c_no = FT.detect_drift("btc", {"L": _live(101500)}, prev=hi, now=TUE)    # 1.5% but diff $1500 ≥ floor → exceeds (floor criterion)
check("1.5% but absolute diff $1500 ≥ $1000 floor → exceeds threshold (floor criterion)", c_no != [], c_no)
_, c_under = FT.detect_drift("btc", {"L": _live(100500)}, prev=hi, now=TUE) # 0.5% and diff $500 < $1000 → under
check("0.5% and diff $500 < $1000 → under threshold", c_under == [], c_under)

print("▶ T-B4b BTC $1000 floor: low-price tier with small percentage still needs $1000")
lo = {"frozen_at": "x", "legs": {"L": _live(50000)}, "drift": {}}
_, c_floor_hit = FT.detect_drift("btc", {"L": _live(51000)}, prev=lo, now=TUE)  # 2% and $1000 → hit
check("low-price tier $1000 absolute diff → exceeds threshold", c_floor_hit != [], c_floor_hit)
_, c_floor_miss = FT.detect_drift("btc", {"L": _live(50600)}, prev=lo, now=TUE)  # 1.2% and $600 → miss
check("low-price tier $600 < $1000 and 1.2% < 2% → under threshold", c_floor_miss == [], c_floor_miss)

print("▶ T-B4c MSTR threshold 3%: three-point pin")
mhi = {"frozen_at": "x", "legs": {"L": _live(100)}, "drift": {}}
_, m_eq = FT.detect_drift("mstr", {"L": _live(103)}, prev=mhi, now=TUE)   # exactly 3.0%
check("MSTR exactly 3% → exceeds threshold", m_eq != [], m_eq)
_, m_just = FT.detect_drift("mstr", {"L": _live(103.1)}, prev=mhi, now=TUE)
check("MSTR just over 3% → exceeds threshold", m_just != [], m_just)
_, m_no = FT.detect_drift("mstr", {"L": _live(102)}, prev=mhi, now=TUE)    # 2% < 3% (no floor for MSTR)
check("MSTR 2% < 3% (no $1000 floor for MSTR) → under threshold", m_no == [], m_no)


print("▶ T-B5 lost-anchor/fetch-failed legs not passed in → keep old frozen, no drift, no false alert")
prev_multi = {"frozen_at": "x", "legs": {"B3-a": _live(60000), "C1-a": _live(57000)},
              "drift": {"B3-a": 0.0, "C1-a": 0.0123}}
# This cycle only B3-a is available (C1 lost-anchor; caller excludes it); C1-a not filled
snap5, ch5 = FT.detect_drift("btc", {"B3-a": _live(60100)}, prev=prev_multi, now=TUE)
check("lost-anchor C1 not in changes (no false alert)", all(c["label"] != "C1-a" for c in ch5), ch5)
check("B3-a micro-drift under threshold → no change", ch5 == [], ch5)

print("▶ T-B5b lost-anchor leg (not filled) keeps old frozen + old frozen_at + old drift, unchanged")
# C1-a lost-anchor (absent from live this cycle) and not filled → preserve frozen price 57000
check("lost-anchor C1-a still in snapshot (not removed)", "C1-a" in snap5["legs"], snap5["legs"])
check("lost-anchor C1-a frozen price preserved at 57000", snap5["legs"]["C1-a"]["price"] == 57000, snap5["legs"])
check("lost-anchor C1-a drift value preserved at 0.0123", snap5["drift"]["C1-a"] == 0.0123, snap5["drift"])
check("no advance and C1 lost-anchor → frozen_at unchanged", snap5["frozen_at"] == "x", snap5)
check("recovery next cycle does not falsely report 'new entry' (C1-a already in prev → not new)",
      FT.detect_drift("btc", {"B3-a": _live(60100), "C1-a": _live(57050)}, prev=snap5, now=TUE)[1] == [],
      "recovery cycle should produce no changes")

print("▶ T-B5c filled legs (in filled_labels) → removed from frozen (existing behavior preserved)")
# B3-a absent from live + in filled_labels → correctly removed; C1-a lost-anchor (not filled) → kept
snap5c, ch5c = FT.detect_drift("btc", {"C1-a": _live(57050)}, prev=prev_multi, now=TUE,
                               filled_labels={"B3-a"})
check("filled B3-a removed from frozen", "B3-a" not in snap5c["legs"], snap5c["legs"])
check("filling a leg produces no alert for that leg", all(c["label"] != "B3-a" for c in ch5c), ch5c)
check("lost-anchor C1-a (not filled) still frozen at 57000", snap5c["legs"]["C1-a"]["price"] == 57000, snap5c["legs"])


print("▶ T-B6 weekly reset (E3): force_weekly → recomputes even under threshold + change(reason=weekly)")
snap6, ch6 = FT.detect_drift("btc", {"B3-a": _live(60100)}, prev=prev, now=MON, force_weekly=True)
check("weekly reset → change entry present", ch6 != [], ch6)
check("weekly reset → reason=weekly", ch6[0]["reason"] == "weekly", ch6)
check("weekly reset → frozen price updated to 60100", snap6["legs"]["B3-a"]["price"] == 60100, snap6)
# Auto-detect Monday without explicit force_weekly
snap6b, ch6b = FT.detect_drift("btc", {"B3-a": _live(60100)}, prev=prev, now=MON)
check("Monday auto-triggers weekly reset (reason=weekly)", ch6b and ch6b[0]["reason"] == "weekly", ch6b)
# Non-Monday + under threshold + no force → no changes
_, ch6c = FT.detect_drift("btc", {"B3-a": _live(60100)}, prev=prev, now=TUE)
check("non-Monday + under threshold → no changes", ch6c == [], ch6c)


print("▶ T-B7 multi-leg merge alert (E5): same-round legs → build_update_message produces one message")
legs = [
    {"label": "B3-a", "old": 60000, "new": 62000, "drift_pct": 0.0333, "anchor": "green_line", "reason": "threshold"},
    {"label": "B3-b", "old": 58000, "new": 60000, "drift_pct": 0.0345, "anchor": "green_line", "reason": "threshold"},
]
msg = FT.build_update_message("btc", "B3", legs)
check("merged message contains both B3-a and B3-b", "B3-a" in msg and "B3-b" in msg, msg)
check("merged message shows old→new price", "$60,000 → $62,000" in msg, msg)
check("merged message contains manual-review notice (does not execute automatically)", "does not execute automatically" in msg, msg)
check("_round_of extracts prefix correctly", FT._round_of("B3-b") == "B3" and FT._round_of("A-1") == "A")


print("▶ T-B8 frozen != ledger: load/save only touch frozen file, written to /tmp")
with tempfile.TemporaryDirectory() as d:
    fp = pathlib.Path(d) / "frozen_tiers.json"
    snap_w = {"btc": {"frozen_at": "t", "legs": {"B3-a": _live(60000)}, "drift": {}}}
    FT.save_frozen(snap_w, fp)
    check("frozen snapshot persisted and can be reloaded", FT.load_frozen(fp) == snap_w)
    check("file is named frozen_tiers.json", fp.exists() and fp.name == "frozen_tiers.json")
    check("no leftover .tmp file", not (pathlib.Path(d) / "frozen_tiers.tmp").exists())
    check("no state.json written to the directory", not (pathlib.Path(d) / "state.json").exists())
    # Missing / corrupt → {}
    miss = pathlib.Path(d) / "nope.json"
    check("missing file → {}", FT.load_frozen(miss) == {})
    bad = pathlib.Path(d) / "bad.json"
    bad.write_text("{half", encoding="utf-8")
    check("corrupt file → {}", FT.load_frozen(bad) == {})


print(f"\n{'=' * 44}\nPassed {PASS} · Failed {FAIL}")
import sys  # noqa: E402
sys.exit(1 if FAIL else 0)
