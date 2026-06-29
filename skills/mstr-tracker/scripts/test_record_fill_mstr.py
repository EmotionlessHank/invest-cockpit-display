"""
test_record_fill_mstr.py — full test suite for record_fill_mstr.py (standalone, no dependencies).
Driven via subprocess against the real CLI (= how the agent actually invokes it). Run: python3 test_record_fill_mstr.py
"""

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

SCRIPT = str(pathlib.Path(__file__).parent / "record_fill_mstr.py")

BASE = {
    "updated": "x", "currency": "USD", "rate_rmb_per_usd": 7.0,
    "held_shares": 0, "invested_usd": 0, "avg_cost_usd": 0,
    "budget_core_usd": 14000, "budget_swan_usd": 14000,
    "rounds": [
        {"id": "T1", "name": "T1 Opening Salvo", "layer": "core", "budget_usd": 6300, "sem": "amber",
         "trigger_kind": "price", "status": "open",
         "plan": [{"label": "T1-a", "price": 130, "usd": 1890, "shares": 14.5, "status": "open"},
                  {"label": "T1-b", "price": 118, "usd": 2520, "shares": 21.4, "status": "open"},
                  {"label": "T1-c", "price": 105, "usd": 1890, "shares": 18, "status": "open"}], "fills": []},
        {"id": "T2", "name": "T2 Green-Line Core", "layer": "core", "budget_usd": 7700, "sem": "green",
         "trigger_kind": "mnav", "status": "open",
         "plan": [{"label": "T2-a", "mnav": 0.78, "price": 101, "usd": 2695, "shares": 27, "status": "open"},
                  {"label": "T2-b", "mnav": 0.70, "price": 91, "usd": 2695, "shares": 30, "status": "open"},
                  {"label": "T2-c", "mnav": 0.65, "price": 84, "usd": 2310, "shares": 27, "status": "open"}], "fills": []},
        {"id": "A", "name": "Swan-A High Probability", "layer": "swan", "budget_usd": 7000, "sem": "red",
         "trigger_kind": "mnav", "status": "wait",
         "plan": [{"label": "A-1", "mnav": 0.70, "price": 90, "usd": 3500, "shares": 39, "status": "open"},
                  {"label": "A-2", "mnav": 0.60, "price": 77, "usd": 3500, "shares": 45, "status": "open"}], "fills": []}
    ],
}

PASS = FAIL = 0


def run(state_path, *args):
    env = {**os.environ, "MSTR_STATE": str(state_path)}
    r = subprocess.run([sys.executable, SCRIPT, *args, "--no-board"], capture_output=True, text=True, env=env)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        res = {"_raw": r.stdout, "_err": r.stderr}
    st = None
    p = pathlib.Path(state_path)
    if p.exists():
        try:
            st = json.loads(p.read_text("utf-8"))
        except Exception:
            st = None
    return res, st


def fixture(tmp, **over):
    s = copy.deepcopy(BASE)
    s.update(over)
    p = pathlib.Path(tmp) / "mstr.json"
    p.write_text(json.dumps(s, ensure_ascii=False), "utf-8")
    return p


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def case(fn):
    print(f"▶ {fn.__name__}: {fn.__doc__.strip()}")
    with tempfile.TemporaryDirectory() as tmp:
        fn(tmp)


def t01_round_price_shares(tmp):
    """round + price + shares: record exactly usd=price×shares"""
    p = fixture(tmp)
    res, st = run(p, "--round", "T2", "--price", "91", "--shares", "30")
    check("claims T2", res.get("ok") and res["round"] == "T2 Green-Line Core", res)
    check("usd=2730", res["usd"] == 2730, res)
    check("batch T2-b", res["batch"] == "T2-b", res)
    check("invested+2730", st["invested_usd"] == 2730, st["invested_usd"])
    check("held 30 sh", st["held_shares"] == 30, st["held_shares"])
    t2 = st["rounds"][1]
    check("audit fields", all(k in t2["fills"][0] for k in ("price", "shares", "usd", "at")), t2["fills"])


def t02_round_price_no_shares(tmp):
    """round + price only → estimate from the planned batch"""
    p = fixture(tmp)
    res, st = run(p, "--round", "T1", "--price", "130")
    check("batch T1-a", res["batch"] == "T1-a", res)
    check("uses planned usd 1890", res["usd"] == 1890, res)
    check("uses planned shares 14.5", res["shares"] == 14.5, res)


def t03_infer_round_by_price(tmp):
    """no round → match to nearest round by price"""
    p = fixture(tmp)
    res, _ = run(p, "--price", "130", "--shares", "14")
    check("lands on T1", res.get("ok") and res["round"] == "T1 Opening Salvo", res)


def t04_fill_to_done(tmp):
    """T1 all three batches filled → done"""
    p = fixture(tmp)
    run(p, "--round", "T1", "--price", "130")
    run(p, "--round", "T1", "--price", "118")
    res, st = run(p, "--round", "T1", "--price", "105")
    check("done", res["round_status"] == "done", res)
    check("T1 all legs filled (not deleted, new schema)",
          all(b["status"] == "filled" for b in st["rounds"][0]["plan"]), st["rounds"][0]["plan"])


def t05_price_invalid(tmp):
    """absurd share price → failure"""
    p = fixture(tmp)
    res, _ = run(p, "--round", "T1", "--price", "5")
    check("ok=false", res.get("ok") is False, res)


def t06_shares_invalid(tmp):
    """invalid shares → failure"""
    p = fixture(tmp)
    r1, _ = run(p, "--round", "T2", "--price", "91", "--shares", "-3")
    r2, _ = run(p, "--round", "T2", "--price", "91", "--shares", "9999")
    check("negative fails", r1.get("ok") is False, r1)
    check("too-large fails", r2.get("ok") is False, r2)


def t07_no_price(tmp):
    """missing fill price → failure"""
    p = fixture(tmp)
    res, _ = run(p, "--round", "T2")
    check("ok=false", res.get("ok") is False, res)


def t08_unknown_round(tmp):
    """unknown round → failure"""
    p = fixture(tmp)
    res, _ = run(p, "--round", "T9", "--price", "100")
    check("ok=false", res.get("ok") is False, res)


def t09_undo(tmp):
    """undo after a fill → exact rollback"""
    p = fixture(tmp)
    before = json.loads(p.read_text("utf-8"))
    run(p, "--round", "T2", "--price", "91", "--shares", "30")
    res, st = run(p, "--undo")
    check("undo ok", res.get("ok"), res)
    check("whole ledger restored", st == before, "diff")


def t10_undo_empty(tmp):
    """undo with no backup → failure"""
    p = fixture(tmp)
    res, _ = run(p, "--undo")
    check("ok=false", res.get("ok") is False, res)


def t11_double_report(tmp):
    """same round, same price duplicate → blocked, --force lets it through"""
    p = fixture(tmp)
    run(p, "--round", "T2", "--price", "91", "--shares", "30")
    res, st = run(p, "--round", "T2", "--price", "91", "--shares", "30")
    check("second is blocked", res.get("ok") is False, res)
    check("ledger not double-counted", st["invested_usd"] == 2730, st["invested_usd"])
    res2, _ = run(p, "--round", "T2", "--price", "91", "--shares", "30", "--force")
    check("--force lets it through", res2.get("ok"), res2)


def t12_budget_cap(tmp):
    """over the $28,000 cap → blocked, --force lets it through"""
    p = fixture(tmp, invested_usd=27000)
    res, st = run(p, "--round", "T2", "--price", "91")  # +2695 > 28000
    check("over budget is blocked", res.get("ok") is False, res)
    check("ledger unchanged", st["invested_usd"] == 27000, st["invested_usd"])
    res2, _ = run(p, "--round", "T2", "--price", "91", "--force")
    check("--force lets it through", res2.get("ok"), res2)


def t13_corrupt_state(tmp):
    """corrupt ledger → graceful error"""
    p = pathlib.Path(tmp) / "mstr.json"
    p.write_text("{ broken ", "utf-8")
    res, _ = run(p, "--round", "T1", "--price", "130")
    check("ok=false with msg", res.get("ok") is False and "msg" in res, res)


def t14_missing_state(tmp):
    """missing ledger → graceful error"""
    res, _ = run(pathlib.Path(tmp) / "nope.json", "--round", "T1", "--price", "130")
    check("ok=false", res.get("ok") is False, res)


def t15_recompute_avg(tmp):
    """after a fill, avg = invested/held"""
    p = fixture(tmp)
    _, st = run(p, "--round", "T2", "--price", "91", "--shares", "30")
    check("avg 91", st["avg_cost_usd"] == round(2730 / 30, 2), st["avg_cost_usd"])


def t16_no_temp_leftover(tmp):
    """no .tmp left after an atomic write"""
    p = fixture(tmp)
    run(p, "--round", "T1", "--price", "130")
    check("no .tmp", not list(pathlib.Path(tmp).glob("*.tmp.json")))


def t17_append_only(tmp):
    """fills are only appended"""
    p = fixture(tmp)
    run(p, "--round", "T1", "--price", "130")
    _, st = run(p, "--round", "T1", "--price", "118")
    check("both fills present", len(st["rounds"][0]["fills"]) == 2, st["rounds"][0]["fills"])


def t19_emits_board(tmp):
    """after a write, by default echo the full MSTR board (unified progress management), with the last line still parseable JSON"""
    p = fixture(tmp)
    env = {**os.environ, "MSTR_STATE": str(p)}
    r = subprocess.run([sys.executable, SCRIPT, "--round", "T1", "--price", "130"], capture_output=True, text=True, env=env)
    lines = r.stdout.strip().splitlines()
    check("output contains the full board", any("MSTR Buy-the-Dip Progress" in ln for ln in lines), r.stdout[:80])
    check("last line is still JSON ok", json.loads(lines[-1]).get("ok") is True, lines[-1] if lines else "")


def t26_board_live_price(tmp):
    """board order price: mNAV batches derived from live NAV-per-share with an ≈ prefix (same basis as the dashboard live price); on a fetch failure, fall back to the static reference price"""
    os.environ["DASH_URL"] = "https://example.test/d"   # avoid signing a real link at import time
    import show_progress_mstr as sp
    orig = sp.mnav.compute
    try:
        sp.mnav.compute = lambda **kw: {"mstr": 120.44, "btc": 64000, "nav_per_share": 138.4, "mnav": 0.87}
        board = sp.build_board(copy.deepcopy(BASE))
        check("T2-a live-derived ≈$108", "T2-a ≈$108" in board, board)
        check("T1-c fixed price, no ≈", "T1-c $105" in board, board)
        check("A-2 live-derived ≈$83", "A-2 ≈$83" in board, board)
        sp.mnav.compute = lambda **kw: {"mstr": None, "btc": None, "nav_per_share": None, "mnav": None}
        board2 = sp.build_board(copy.deepcopy(BASE))
        check("fetch failure falls back to static price", "T2-a $101" in board2, board2)
    finally:
        sp.mnav.compute = orig


def t18_mnav_fallback(tmp):
    """mnav.compute: price source fetch failure → mnav None, no exception; offline-provided values → formula correct"""
    import importlib
    m = importlib.import_module("mnav")
    m.get_mstr_price = lambda: None   # simulate an unreachable price source
    m.get_btc_price = lambda: None
    r0 = m.compute()
    check("fetch failure mnav=None, no throw", r0["mnav"] is None, r0)
    r1 = m.compute(mstr_price=126.55, btc_price=64000)   # offline-provided, no network
    # Basis = navPremiumDiluted: do not subtract net debt, use diluted shares
    nps = (64000 * m.BTC_HOLDINGS) / m.DILUTED_SHARES
    check("offline mNAV formula correct", abs(r1["mnav"] - round(126.55 / nps, 4)) < 1e-6, r1)


def t20_concurrent_locked(tmp):
    """concurrent fills → file lock serializes them; held/invested accumulate exactly (no lost updates)"""
    p = fixture(tmp)
    env = {**os.environ, "MSTR_STATE": str(p)}
    n = 12
    procs = [subprocess.Popen([sys.executable, SCRIPT, "--round", "T1", "--price", "130",
                               "--shares", "1", "--force", "--no-board"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
             for _ in range(n)]
    for pr in procs:
        pr.wait()
    st = json.loads(pathlib.Path(p).read_text("utf-8"))
    check(f"{n} concurrent fills, no lost held", st["held_shares"] == n, st["held_shares"])
    check("invested accumulates exactly", st["invested_usd"] == n * 130, st["invested_usd"])


def t21_backdate_fill(tmp):
    """backdate a historical fill: --at writes fill.at (bare date); invalid dates are blocked and not persisted"""
    p = fixture(tmp)
    res, st = run(p, "--round", "T2", "--price", "91", "--shares", "30", "--at", "2026-05-20")
    check("ok", res.get("ok") is True, res)
    check("at=bare date", st["rounds"][1]["fills"][0]["at"] == "2026-05-20", st["rounds"][1]["fills"])
    for bad in ("2999-01-01", "2026/05/20", "2019-12-31", "2026-02-30"):
        res2, _ = run(p, "--round", "T1", "--price", "130", "--at", bad)
        check(f"{bad} blocked", res2.get("ok") is False, res2)


def t22_no_at_keeps_timestamp(tmp):
    """without --at → at is still a full ISO timestamp (original behavior, zero change)"""
    p = fixture(tmp)
    _, st = run(p, "--round", "T1", "--price", "130")
    check("at contains a T time", "T" in st["rounds"][0]["fills"][0]["at"], st["rounds"][0]["fills"])


def t23_standalone_basic(tmp):
    """standalone buy (--standalone): lands in top-level standalone_fills, touches no round, counted into holdings/invested/avg"""
    p = fixture(tmp)
    res, st = run(p, "--standalone", "--price", "130", "--shares", "5")
    check("ok", res.get("ok") is True, res)
    check("lands in standalone_fills", len(st.get("standalone_fills", [])) == 1, st.get("standalone_fills"))
    check("round fills all empty", all(not r["fills"] for r in st["rounds"]), st["rounds"])
    check("round plan not consumed", len(st["rounds"][0]["plan"]) == 3, st["rounds"][0]["plan"])
    check("invested +650", st["invested_usd"] == 650, st["invested_usd"])
    check("held +5", st["held_shares"] == 5, st["held_shares"])
    check("avg 130", st["avg_cost_usd"] == 130, st["avg_cost_usd"])
    check("audit fields include at", "at" in st["standalone_fills"][0], st["standalone_fills"])


def t24_standalone_requires_shares(tmp):
    """standalone buy missing shares → refused (no batch to estimate from), not persisted"""
    p = fixture(tmp)
    before = p.read_text("utf-8")
    res, _ = run(p, "--standalone", "--price", "130")
    check("ok=false with msg", res.get("ok") is False and "msg" in res, res)
    check("ledger unchanged", p.read_text("utf-8") == before)


def t25_standalone_guards(tmp):
    """standalone buy: same-price duplicate blocked, --force lets through; over-budget blocked; undoable; backdatable"""
    p = fixture(tmp)
    run(p, "--standalone", "--price", "130", "--shares", "5")
    res, st = run(p, "--standalone", "--price", "130", "--shares", "5")
    check("duplicate blocked", res.get("ok") is False, res)
    check("not double-counted", st["invested_usd"] == 650, st["invested_usd"])
    res2, _ = run(p, "--standalone", "--price", "130", "--shares", "5", "--force")
    check("--force lets it through", res2.get("ok") is True, res2)
    res3, st3 = run(p, "--undo")
    check("undo rolls back", res3.get("ok") is True and st3["invested_usd"] == 650, (res3, st3["invested_usd"]))
    res4, st4 = run(p, "--standalone", "--price", "120", "--shares", "2", "--at", "2026-05-20")
    check("backdate combo ok", res4.get("ok") is True, res4)
    check("at=bare date", st4["standalone_fills"][-1]["at"] == "2026-05-20", st4["standalone_fills"][-1])
    p2 = fixture(tmp, invested_usd=27900)
    res5, _ = run(p2, "--standalone", "--price", "130", "--shares", "5")  # +650 > 28000
    check("over budget blocked", res5.get("ok") is False, res5)


def t27_label_precise_claim(tmp):
    """--label exact-claim a leg: status→filled, fill.label back-links, leg not deleted"""
    p = fixture(tmp)
    res, st = run(p, "--round", "T2", "--price", "91", "--label", "T2-b", "--shares", "30")
    check("ok, claims T2-b", res.get("ok") and res["batch"] == "T2-b", res)
    t2 = st["rounds"][1]
    t2b = next(b for b in t2["plan"] if b["label"] == "T2-b")
    check("T2-b status=filled", t2b["status"] == "filled", t2b)
    check("T2-b leg still in plan (not deleted)", len(t2["plan"]) == 3, t2["plan"])
    check("T2-a/T2-c still open", next(b for b in t2["plan"] if b["label"] == "T2-a")["status"] == "open")
    check("fill.label=T2-b", t2["fills"][0]["label"] == "T2-b", t2["fills"])


def t28_label_whitelist(tmp):
    """label whitelist: a nonexistent / already-filled label → refused (without --force)"""
    p = fixture(tmp)
    res, st = run(p, "--round", "T2", "--price", "91", "--label", "T2-z", "--shares", "30")
    check("nonexistent label → ok=false", res.get("ok") is False and "msg" in res, res)
    check("not persisted", st["invested_usd"] == 0, st["invested_usd"])
    run(p, "--round", "T2", "--price", "91", "--label", "T2-b", "--shares", "30")
    res2, _ = run(p, "--round", "T2", "--price", "91", "--label", "T2-b", "--shares", "30")
    check("already-filled label duplicate → blocked", res2.get("ok") is False, res2)
    # Isolation assertion (pin the label filled-guard): use a different price (88, never recorded → the same-price
    # dedup guard will not fire), and without --force re-claim the already-filled T2-b → it must be stopped by the
    # filled-guard, with the message reflecting "already filled" rather than "already has a fill recorded".
    res2b, _ = run(p, "--round", "T2", "--price", "88", "--label", "T2-b", "--shares", "30")
    m = res2b.get("msg", "")
    check("different price re-claiming an already-filled label → filled-guard (not same-price dedup)",
          res2b.get("ok") is False and "filled" in m and "already has a fill recorded" not in m, res2b)
    res3, _ = run(p, "--round", "T2", "--price", "91", "--label", "T2-b", "--shares", "30", "--force")
    check("--force overrides", res3.get("ok") is True, res3)


def t29_status_derive_undo(tmp):
    """round status derivation partial/done + undo rollback"""
    p = fixture(tmp)
    before = json.loads(p.read_text("utf-8"))
    r1, _ = run(p, "--round", "T1", "--price", "130", "--label", "T1-a")
    check("one leg filled → partial", r1["round_status"] == "partial", r1)
    res, st = run(p, "--undo")
    check("undo restores whole ledger (incl. status back to open)", st == before, "diff")


CASES = [t01_round_price_shares, t02_round_price_no_shares, t03_infer_round_by_price, t04_fill_to_done,
         t05_price_invalid, t06_shares_invalid, t07_no_price, t08_unknown_round, t09_undo, t10_undo_empty,
         t11_double_report, t12_budget_cap, t13_corrupt_state, t14_missing_state, t15_recompute_avg,
         t16_no_temp_leftover, t17_append_only, t18_mnav_fallback, t19_emits_board, t20_concurrent_locked,
         t21_backdate_fill, t22_no_at_keeps_timestamp,
         t23_standalone_basic, t24_standalone_requires_shares, t25_standalone_guards,
         t26_board_live_price,
         t27_label_precise_claim, t28_label_whitelist, t29_status_derive_undo]

if __name__ == "__main__":
    for c in CASES:
        case(c)
    print(f"\n{'='*40}\nPASS {PASS} · FAIL {FAIL}")
    sys.exit(1 if FAIL else 0)
