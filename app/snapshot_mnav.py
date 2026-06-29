#!/usr/bin/env python3
"""
snapshot_mnav.py — daily mNAV snapshot (scheduled cron job).

Computes today's mNAV (definition: saylortracker navPremiumDiluted, no-debt / diluted-shares,
reusing the mnav single source of truth) and appends it to ~/.cockpit/mstr-mnav-history.jsonl.
**Idempotent** (existing row for today is overwritten atomically).
Holdings and diluted share count are injected by cron via `source ~/.cockpit/mstr-inputs.env`;
if the price source is unreachable the snapshot is skipped — no value is fabricated.

Usage: python3 snapshot_mnav.py   # write today's snapshot
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mnav  # noqa: E402

HIST = os.environ.get("MSTR_MNAV_HISTORY", os.path.expanduser("~/.cockpit/mstr-mnav-history.jsonl"))


def append_snapshot(rec, path=HIST):
    """Idempotent append: strip any existing row for the same date, then append rec. Atomic write."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    if json.loads(ln).get("date") == rec["date"]:
                        continue
                except Exception:
                    continue
                lines.append(ln)
    lines.append(json.dumps(rec, ensure_ascii=False))
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def main():
    q = mnav.compute()
    if q["mnav"] is None:
        print("mNAV fetch failed (price source unreachable), snapshot skipped")
        return 1
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = {"date": date, "mnav": q["mnav"], "mstr": q["mstr"],
           "btc": round(q["btc"], 2),
           "holdings": mnav.BTC_HOLDINGS, "diluted_m": round(mnav.DILUTED_SHARES / 1e6, 3)}
    # resolve path at runtime (MSTR_MNAV_HISTORY override supported; default ~/.cockpit/mstr-mnav-history.jsonl)
    path = os.environ.get("MSTR_MNAV_HISTORY", os.path.expanduser("~/.cockpit/mstr-mnav-history.jsonl"))
    append_snapshot(rec, path)
    print(f"Snapshot {date}: mNAV {rec['mnav']} · MSTR ${rec['mstr']} · holdings {rec['holdings']:.0f} · diluted {rec['diluted_m']}M")
    return 0


if __name__ == "__main__":
    sys.exit(main())
