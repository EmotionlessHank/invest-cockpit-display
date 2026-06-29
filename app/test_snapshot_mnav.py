"""
test_snapshot_mnav.py — daily mNAV snapshot tests.
Validates: idempotency (same-day overwrite, no duplicates), failure-skips-write, atomic append,
complete fields. Uses /tmp + monkeypatching throughout; no network calls.
"""
import json
import os
import sys
import tempfile
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mnav  # noqa: E402
import snapshot_mnav  # noqa: E402

_fail = 0


def check(name, cond, ctx=""):
    global _fail
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"  {ctx}"))
    if not cond:
        _fail += 1


def _lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def t01_append_and_idempotent():
    """Multiple writes for the same date → only one row retained (overwritten); different dates accumulate."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "h.jsonl")
        snapshot_mnav.append_snapshot({"date": "2026-06-26", "mnav": 0.63, "mstr": 82.0}, p)
        snapshot_mnav.append_snapshot({"date": "2026-06-26", "mnav": 0.64, "mstr": 82.5}, p)  # same-day overwrite
        snapshot_mnav.append_snapshot({"date": "2026-06-29", "mnav": 0.62, "mstr": 81.0}, p)
        rows = _lines(p)
        check("same-day deduped + cross-day accumulated = 2 rows", len(rows) == 2, str(rows))
        d26 = [r for r in rows if r["date"] == "2026-06-26"]
        check("same-day overwrite keeps latest value 0.64", len(d26) == 1 and d26[0]["mnav"] == 0.64, str(d26))
        check("cross-day row 2026-06-29 present", any(r["date"] == "2026-06-29" for r in rows), str(rows))


def t02_no_temp_leftover():
    """No .tmp file left over after atomic write."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "h.jsonl")
        snapshot_mnav.append_snapshot({"date": "2026-06-29", "mnav": 0.62}, p)
        check("no .tmp leftover", not os.path.exists(p + ".tmp"), os.listdir(d))


def t03_main_skips_on_fail():
    """mNAV fetch failure → main returns 1 and does not write file."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "h.jsonl")
        os.environ["MSTR_MNAV_HISTORY"] = p
        snapshot_mnav.HIST = p
        orig = mnav.compute
        mnav.compute = lambda *a, **k: {"mstr": None, "btc": None, "nav_per_share": None, "mnav": None}
        try:
            rc = snapshot_mnav.main()
        finally:
            mnav.compute = orig
        check("fetch failure: main returns 1", rc == 1, str(rc))
        check("fetch failure: no file written", not os.path.exists(p), "file should not exist")


def t04_main_writes_full_record():
    """Success → writes complete record with fields {date, mnav, mstr, btc, holdings, diluted_m}."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "h.jsonl")
        os.environ["MSTR_MNAV_HISTORY"] = p
        snapshot_mnav.HIST = p
        orig = mnav.compute
        mnav.compute = lambda *a, **k: {"mstr": 82.31, "btc": 59287.02, "nav_per_share": 129.27, "mnav": 0.6367}
        try:
            rc = snapshot_mnav.main()
        finally:
            mnav.compute = orig
        check("success: main returns 0", rc == 0, str(rc))
        rows = _lines(p)
        check("one complete record with all required fields",
              len(rows) == 1 and {"date", "mnav", "mstr", "btc", "holdings", "diluted_m"} <= set(rows[0]), str(rows))
        check("mnav=0.6367", rows[0]["mnav"] == 0.6367, str(rows[0]))


for fn in (t01_append_and_idempotent, t02_no_temp_leftover,
           t03_main_skips_on_fail, t04_main_writes_full_record):
    print(f"▶ {fn.__name__}: {fn.__doc__.strip().splitlines()[0]}")
    fn()

print("✓ All passed" if _fail == 0 else f"✗ Failed {_fail}")
sys.exit(1 if _fail else 0)
