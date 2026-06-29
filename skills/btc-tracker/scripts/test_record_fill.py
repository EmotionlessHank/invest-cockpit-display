"""
test_record_fill.py — full test suite for record_fill.py (standalone, no third-party deps).

Driver: subprocess runs the real CLI (= the command the agent actually invokes), asserting on both the stdout JSON and the persisted state.
Run: python3 test_record_fill.py   (all green -> exit 0; any failure -> exit 1)

Note: tier names ("B2 core"...), track names ("Total budget used"...) and the funding-source samples below
are state.json schema values shared with the seeds (English data contract); they are round-tripped test data.
"""

import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

SCRIPT = str(pathlib.Path(__file__).parent / "record_fill.py")

BASE = {
    "updated": "2026-06-03T00:00:00+10:00",
    "held_btc": 0.1, "invested_rmb": 50000, "avg_cost_usd": 68000,
    "budget_total_rmb": 200000, "rate_rmb_per_usdt": 7.0,
    "tracks": [
        {"name": "Total budget used", "used": 50000, "quota": 200000, "color": "green"},
        {"name": "DCA", "used": 3000, "quota": 30000, "color": "blue"},
        {"name": "B core dip-buy", "used": 0, "quota": 88000, "color": "amber"},
        {"name": "C deep-dip reserve", "used": 0, "quota": 30135, "color": "red"},
    ],
    "tiers": [
        {"name": "B2 core", "lo": 60000, "hi": 66000, "amount": 28000, "status": "open", "sem": "amber",
         "plan": [{"label": "B2-a", "price": 64000, "rmb": 14000, "status": "open"},
                  {"label": "B2-b", "price": 61000, "rmb": 14000, "status": "open"}], "fills": []},
        {"name": "C2 deep dip", "lo": 48000, "hi": 52000, "amount": 14135, "status": "wait", "sem": "red",
         "plan": [{"label": "C2-a", "price": 51000, "rmb": 7000, "status": "open"},
                  {"label": "C2-b", "price": 48000, "rmb": 7000, "status": "open"}], "fills": []},
    ],
    "onchain": [],
}

PASS = FAIL = 0


def run(state_path, *args):
    """Run record_fill.py, return (stdout JSON dict, persisted state or None)."""
    env = {**os.environ, "BTC_STATE": str(state_path)}
    r = subprocess.run([sys.executable, SCRIPT, *args, "--no-board"], capture_output=True, text=True, env=env)
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        res = {"_raw": r.stdout, "_err": r.stderr}
    st = None
    p = pathlib.Path(state_path)
    if p.exists():
        try:
            st = json.loads(p.read_text("utf-8"))  # return None if a corrupted ledger fails to read back, don't crash the framework
        except Exception:
            st = None
    return res, st


def fixture(tmp, **over):
    s = copy.deepcopy(BASE)
    s.update(over)
    p = pathlib.Path(tmp) / "state.json"
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


# ---------------- cases ----------------

def t01_fill_match_plan(tmp):
    """fill without an amount -> match the closest leg in the plan"""
    p = fixture(tmp)
    res, st = run(p, "--price", "64000")
    check("ok & claimed B2", res.get("ok") and res["claimed"] == "B2 core", res)
    check("returns label B2-a (closest open leg by price)", res.get("label") == "B2-a", res)
    check("amount taken from plan 14000", res["rmb"] == 14000, res)
    check("partial", res["tier_status"] == "partial", res)
    check("invested +14000", st["invested_rmb"] == 64000, st["invested_rmb"])
    check("B-dip track +14000", st["tracks"][2]["used"] == 14000)
    b2 = st["tiers"][0]
    # New schema: a fill does not delete the leg, that leg's status -> filled (still in plan)
    b2a = next(p for p in b2["plan"] if p["price"] == 64000)
    check("64000 leg still in plan with status=filled (not deleted)", b2a.get("status") == "filled", b2["plan"])
    check("61000 leg still open", next(p for p in b2["plan"] if p["price"] == 61000)["status"] == "open")
    check("fill carries label + audit fields btc/usd/at",
          b2["fills"][0].get("label") == "B2-a"
          and all(k in b2["fills"][0] for k in ("btc", "usd", "at")), b2["fills"])


def t02_fill_to_done(tmp):
    """both legs filled -> done"""
    p = fixture(tmp)
    run(p, "--price", "64000")
    res, st = run(p, "--price", "61000")
    check("done", res["tier_status"] == "done", res)
    check("all legs filled (not deleted, new schema)",
          all(p_["status"] == "filled" for p_ in st["tiers"][0]["plan"]), st["tiers"][0]["plan"])
    check("invested 50000+28000", st["invested_rmb"] == 78000, st["invested_rmb"])


def t03_fill_explicit_rmb(tmp):
    """explicit --rmb overrides the planned amount"""
    p = fixture(tmp)
    res, st = run(p, "--price", "64000", "--rmb", "5000")
    check("uses 5000", res["rmb"] == 5000, res)
    check("invested +5000", st["invested_rmb"] == 55000, st["invested_rmb"])
    # explicit --rmb still claims the closest open leg (B2-a@64000) and sets it filled, leg not deleted
    b2a = next(p_ for p_ in st["tiers"][0]["plan"] if p_["price"] == 64000)
    check("closest leg B2-a claimed and set filled (not deleted)", b2a["status"] == "filled" and len(st["tiers"][0]["plan"]) == 2)


def t04_out_of_range(tmp):
    """price out of range -> graceful failure, no write"""
    p = fixture(tmp)
    before = p.read_text("utf-8")
    res, st = run(p, "--price", "100000")
    check("ok=false", res.get("ok") is False, res)
    check("ledger unchanged", p.read_text("utf-8") == before)


def t05_price_invalid(tmp):
    """absurd price -> validation failure"""
    p = fixture(tmp)
    res, _ = run(p, "--price", "50")
    check("ok=false", res.get("ok") is False, res)


def t06_dca(tmp):
    """DCA recording"""
    p = fixture(tmp)
    res, st = run(p, "--dca", "--rmb", "3000")
    check("ok", res.get("ok"), res)
    check("invested +3000", st["invested_rmb"] == 53000, st["invested_rmb"])
    check("DCA track 3000+3000", st["tracks"][1]["used"] == 6000)


def t07_dca_no_amount(tmp):
    """DCA missing amount -> failure"""
    p = fixture(tmp)
    res, _ = run(p, "--dca")
    check("ok=false", res.get("ok") is False, res)


def t08_rmb_invalid(tmp):
    """invalid amount (negative / huge) -> failure"""
    p = fixture(tmp)
    r1, _ = run(p, "--price", "64000", "--rmb", "-5")
    r2, _ = run(p, "--price", "64000", "--rmb", "999999")
    check("negative fails", r1.get("ok") is False, r1)
    check("huge fails", r2.get("ok") is False, r2)


def t09_undo(tmp):
    """undo after a fill -> exact rollback"""
    p = fixture(tmp)
    before = json.loads(p.read_text("utf-8"))
    run(p, "--price", "64000")
    res, st = run(p, "--undo")
    check("undo ok", res.get("ok"), res)
    check("invested back to 50000", st["invested_rmb"] == 50000, st["invested_rmb"])
    check("whole ledger restored field-by-field", st == before, "diff")


def t10_undo_empty(tmp):
    """undo with no backup -> failure"""
    p = fixture(tmp)
    res, _ = run(p, "--undo")
    check("ok=false", res.get("ok") is False, res)


def t11_double_report(tmp):
    """duplicate report at the same price -> guard blocks, --force lets it through"""
    p = fixture(tmp)
    run(p, "--price", "64000", "--rmb", "14000")
    res, st = run(p, "--price", "64000", "--rmb", "14000")
    check("second one blocked", res.get("ok") is False, res)
    check("ledger not double-added", st["invested_rmb"] == 64000, st["invested_rmb"])
    res2, st2 = run(p, "--price", "64000", "--rmb", "14000", "--force")
    check("--force lets it through", res2.get("ok"), res2)
    check("after force +14000", st2["invested_rmb"] == 78000, st2["invested_rmb"])


def t12_budget_cap(tmp):
    """over the ¥200k cap -> guard blocks, --force lets it through"""
    p = fixture(tmp, invested_rmb=195000,
                tracks=copy.deepcopy(BASE["tracks"]))
    res, st = run(p, "--price", "64000", "--rmb", "14000")  # 195000+14000>200000
    check("over budget blocked", res.get("ok") is False, res)
    check("ledger unchanged", st["invested_rmb"] == 195000, st["invested_rmb"])
    res2, _ = run(p, "--price", "64000", "--rmb", "14000", "--force")
    check("--force lets it through", res2.get("ok"), res2)


def t13_corrupt_state(tmp):
    """corrupted ledger -> graceful error, no stack trace"""
    p = pathlib.Path(tmp) / "state.json"
    p.write_text("{ broken json ", "utf-8")
    res, _ = run(p, "--price", "64000")
    check("ok=false with msg", res.get("ok") is False and "msg" in res, res)


def t14_missing_state(tmp):
    """ledger does not exist -> graceful error"""
    p = pathlib.Path(tmp) / "nope.json"
    res, _ = run(p, "--price", "64000")
    check("ok=false", res.get("ok") is False, res)


def t15_recompute_avg(tmp):
    """average cost after a fill = round(invested/rate/held)"""
    p = fixture(tmp)
    _, st = run(p, "--price", "64000")
    exp = round(st["invested_rmb"] / st["rate_rmb_per_usdt"] / st["held_btc"])
    check("average matches", st["avg_cost_usd"] == exp, (st["avg_cost_usd"], exp))


def t16_no_temp_leftover(tmp):
    """no .tmp leftover after the atomic write"""
    p = fixture(tmp)
    run(p, "--price", "64000")
    leftovers = list(pathlib.Path(tmp).glob("*.tmp.json"))
    check("no .tmp leftover", not leftovers, leftovers)


def t17_append_only(tmp):
    """a fill only appends, never overwrites existing fills"""
    p = fixture(tmp)
    run(p, "--price", "64000")
    _, st = run(p, "--price", "61000")
    check("both entries in fills", len(st["tiers"][0]["fills"]) == 2, st["tiers"][0]["fills"])


def t19_emits_board(tmp):
    """by default a recording hands back the full board (unified progress management), last line is still parseable JSON"""
    p = fixture(tmp)
    env = {**os.environ, "BTC_STATE": str(p)}
    r = subprocess.run([sys.executable, SCRIPT, "--price", "64000"], capture_output=True, text=True, env=env)
    lines = r.stdout.strip().splitlines()
    check("output contains the full board", any("BTC dip progress" in ln for ln in lines), r.stdout[:80])
    check("last line is still JSON ok", json.loads(lines[-1]).get("ok") is True, lines[-1] if lines else "")


def t18_rate_zero(tmp):
    """rate of 0 -> graceful error (no ZeroDivisionError, no silent crash)"""
    p = fixture(tmp, rate_rmb_per_usdt=0)
    res, st = run(p, "--price", "64000")
    check("fill ok=false with msg", res.get("ok") is False and "msg" in res, res)
    res2, _ = run(p, "--dca", "--rmb", "3000")
    check("dca ok=false with msg", res2.get("ok") is False and "msg" in res2, res2)
    check("ledger not corrupted", st["invested_rmb"] == 50000, st["invested_rmb"])


def t20_extra_fund_basic(tmp):
    """extra-funding injection -> separate-pool append, does not touch invested/tracks/budget"""
    p = fixture(tmp)
    res, st = run(p, "--extra-fund", "--rmb", "50000", "--source", "year-end-bonus")
    check("ok", res.get("ok") is True, res)
    check("extra_funding has one entry", len(st.get("extra_funding", [])) == 1, st.get("extra_funding"))
    check("source/amount correct", st["extra_funding"][0]["source"] == "year-end-bonus" and st["extra_funding"][0]["amount"] == 50000)
    check("carries timestamp at", "at" in st["extra_funding"][0])
    check("invested unchanged", st["invested_rmb"] == 50000, st["invested_rmb"])
    check("budget unchanged", st["budget_total_rmb"] == 200000, st["budget_total_rmb"])
    check("total-budget track unchanged", st["tracks"][0]["used"] == 50000)


def t21_extra_fund_no_source(tmp):
    """extra funding missing source -> graceful error, no write"""
    p = fixture(tmp)
    res, st = run(p, "--extra-fund", "--rmb", "50000")
    check("ok=false with msg", res.get("ok") is False and "msg" in res, res)
    check("not persisted", not st.get("extra_funding"), st.get("extra_funding"))


def t22_extra_fund_bad_amount(tmp):
    """invalid extra-funding amount (0 / over EXTRA_MAX) -> blocked, no write"""
    p = fixture(tmp)
    res0, _ = run(p, "--extra-fund", "--rmb", "0", "--source", "x")
    check("0 blocked", res0.get("ok") is False, res0)
    res_big, st = run(p, "--extra-fund", "--rmb", "2000000", "--source", "x")
    check("over EXTRA_MAX blocked", res_big.get("ok") is False, res_big)
    check("neither persisted", not st.get("extra_funding"), st.get("extra_funding"))


def t23_extra_fund_over_200k(tmp):
    """extra funding may exceed 200K (separate pool not bound by the main-position hard cap)"""
    p = fixture(tmp)
    res, st = run(p, "--extra-fund", "--rmb", "300000", "--source", "house-sale")
    check("300k ok (not blocked by RMB_MAX)", res.get("ok") is True, res)
    check("invested still unchanged", st["invested_rmb"] == 50000, st["invested_rmb"])
    check("extra pool cumulative 300k", res.get("total_extra_rmb") == 300000, res)


def t24_extra_fund_undo(tmp):
    """an extra-funding injection can be undone -> extra pool restored"""
    p = fixture(tmp)
    run(p, "--extra-fund", "--rmb", "50000", "--source", "year-end-bonus")
    res, st = run(p, "--undo")
    check("undo ok", res.get("ok") is True, res)
    check("extra pool restored to empty", not st.get("extra_funding"), st.get("extra_funding"))


def t25_concurrent_dca_locked(tmp):
    """concurrent DCAs -> file-lock serialization, invested accumulates exactly (no lost updates)"""
    p = fixture(tmp)
    env = {**os.environ, "BTC_STATE": str(p)}
    n = 12
    procs = [subprocess.Popen([sys.executable, SCRIPT, "--dca", "--rmb", "1000", "--no-board"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
             for _ in range(n)]
    for pr in procs:
        pr.wait()
    st = json.loads(pathlib.Path(p).read_text("utf-8"))
    check(f"{n} concurrent DCAs, invested with no loss", st["invested_rmb"] == 50000 + n * 1000, st["invested_rmb"])
    dca = next(t for t in st["tracks"] if t["name"] == "DCA")
    check("DCA track accumulates exactly", dca["used"] == 3000 + n * 1000, dca["used"])


def t26_backdate_fill(tmp):
    """backdate a historical fill: --at YYYY-MM-DD writes fill.at (bare date, no fabricated time)"""
    p = fixture(tmp)
    res, st = run(p, "--price", "64000", "--at", "2026-05-20")
    check("ok", res.get("ok") is True, res)
    check("at=bare date", st["tiers"][0]["fills"][0]["at"] == "2026-05-20", st["tiers"][0]["fills"])


def t27_backdate_extra_fund(tmp):
    """backdate extra funding: --at writes extra_funding.at"""
    p = fixture(tmp)
    res, st = run(p, "--extra-fund", "--rmb", "50000", "--source", "year-end-bonus", "--at", "2025-12-31")
    check("ok", res.get("ok") is True, res)
    check("at=2025-12-31", st["extra_funding"][0]["at"] == "2025-12-31", st.get("extra_funding"))


def t28_backdate_invalid(tmp):
    """invalid backdate (future / wrong format / before 2020 / not a real calendar day) -> blocked, no write"""
    p = fixture(tmp)
    before = p.read_text("utf-8")
    for bad in ("2999-01-01", "2026/05/20", "2019-12-31", "2026-02-30"):
        res, _ = run(p, "--price", "64000", "--at", bad)
        check(f"{bad} blocked", res.get("ok") is False, res)
    check("ledger unchanged", p.read_text("utf-8") == before)


def t29_backdate_dca(tmp):
    """backdating DCA (allowed once there is a per-entry record): goes into dca_fills as a bare date, but does not light up the current week"""
    p = fixture(tmp)
    res, st = run(p, "--dca", "--rmb", "3000", "--at", "2026-05-20")
    check("ok and msg notes the backdate", res.get("ok") is True and "backdated" in res.get("msg", ""), res)
    check("invested +3000", st["invested_rmb"] == 53000, st["invested_rmb"])
    check("dca_fills at=bare date", st["dca_fills"][0]["at"] == "2026-05-20", st.get("dca_fills"))
    check("backdate does not write dca_last_at (does not light up the week)", "dca_last_at" not in st, st.get("dca_last_at"))
    res2, _ = run(p, "--dca", "--rmb", "3000", "--at", "2999-01-01")
    check("future date still blocked", res2.get("ok") is False, res2)


def t30_no_at_keeps_timestamp(tmp):
    """without --at -> at is still a full ISO timestamp (original behavior unchanged)"""
    p = fixture(tmp)
    _, st = run(p, "--price", "64000")
    check("at contains T time", "T" in st["tiers"][0]["fills"][0]["at"], st["tiers"][0]["fills"])


def t31_dca_writes_last_at(tmp):
    """DCA recording writes dca_last_at (the this-week check) + dca_fills per-entry flow; undo rolls back the whole thing"""
    p = fixture(tmp)
    res, st = run(p, "--dca", "--rmb", "3000")
    check("dca ok", res.get("ok") is True, res)
    check("dca_last_at written and contains a time", "T" in st.get("dca_last_at", ""), st.get("dca_last_at"))
    check("dca_fills has one entry with audit fields", len(st.get("dca_fills", [])) == 1
          and all(k in st["dca_fills"][0] for k in ("rmb", "btc", "at")), st.get("dca_fills"))
    run(p, "--dca", "--rmb", "2000")
    _, st_b = run(p, "--undo")
    check("undo rolls back only the last entry (append-only restore)", len(st_b.get("dca_fills", [])) == 1
          and st_b["dca_fills"][0]["rmb"] == 3000, st_b.get("dca_fills"))


def t32_fill_not_touch_dca(tmp):
    """a dip fill does not write dca_last_at / dca_fills (path isolation)"""
    p = fixture(tmp)
    _, st = run(p, "--price", "64000")
    check("no dca_last_at", "dca_last_at" not in st)
    check("no dca_fills", "dca_fills" not in st)


# ---------------- sub-label --label ----------------

def t33_label_precise_claim(tmp):
    """--label exact-claims a specific leg: status -> filled, fill.label back-links, leg not deleted"""
    p = fixture(tmp)
    res, st = run(p, "--price", "61000", "--label", "B2-b")
    check("ok, claimed B2-b", res.get("ok") and res.get("label") == "B2-b", res)
    b2 = st["tiers"][0]
    b2b = next(x for x in b2["plan"] if x["label"] == "B2-b")
    check("B2-b status=filled", b2b["status"] == "filled", b2b)
    check("B2-b leg still in plan (not deleted)", len(b2["plan"]) == 2, b2["plan"])
    check("B2-a still open (not mis-claimed)", next(x for x in b2["plan"] if x["label"] == "B2-a")["status"] == "open")
    check("fill.label = B2-b", b2["fills"][0]["label"] == "B2-b", b2["fills"])


def t34_label_whitelist(tmp):
    """label whitelist: a non-existent label / an already-filled label -> rejected (without --force), readable error"""
    p = fixture(tmp)
    # non-existent label
    res, st = run(p, "--price", "64000", "--label", "B2-z")
    check("non-existent label -> ok=false with msg", res.get("ok") is False and "msg" in res, res)
    check("non-existent label not persisted", st["invested_rmb"] == 50000, st["invested_rmb"])
    # fill B2-a first, then re-claim with --label B2-a -> already-filled rejection
    run(p, "--price", "64000", "--label", "B2-a")
    res2, st2 = run(p, "--price", "64000", "--label", "B2-a")
    check("already-filled label re-claim -> blocked", res2.get("ok") is False, res2)
    # Isolation assertion (pins the label filled-guard): use a different price within the B2 range (62000 unrecorded -> same-price dedupe won't fire),
    # re-claim the already-filled B2-a without --force -> must be caught by the filled-guard, error reflects "already filled" not "duplicate / already recorded"
    res2b, _ = run(p, "--price", "62000", "--label", "B2-a")
    m = res2b.get("msg", "")
    check("re-claiming an already-filled label at a different price -> filled-guard (not same-price dedupe)",
          res2b.get("ok") is False and "filled" in m and "already has a recorded fill" not in m, res2b)
    # --force overrides
    res3, _ = run(p, "--price", "64000", "--label", "B2-a", "--force")
    check("--force overrides the already-filled label", res3.get("ok") is True, res3)


def t35_no_label_only_open(tmp):
    """no-label fallback: --price alone matches by price among open legs only (won't mis-claim an already-filled leg)"""
    p = fixture(tmp)
    # fill B2-a@64000 first
    run(p, "--price", "64000", "--label", "B2-a")
    # no label, price close to 64000 -> should claim the remaining open leg B2-b (won't re-claim B2-a)
    res, st = run(p, "--price", "63000")
    check("closest match among open legs only -> claims B2-b", res.get("label") == "B2-b", res)
    b2 = st["tiers"][0]
    check("both legs filled", all(x["status"] == "filled" for x in b2["plan"]), b2["plan"])


def t36_tier_full(tmp):
    """tier full guard: with all legs filled and no open leg, --price without an amount -> friendly tier-full notice"""
    p = fixture(tmp)
    run(p, "--price", "64000", "--label", "B2-a")
    run(p, "--price", "61000", "--label", "B2-b")
    # another fill with no amount and no label -> no open leg to claim and no amount -> tier-full notice
    res, _ = run(p, "--price", "62000")
    check("tier full and no amount -> ok=false with msg", res.get("ok") is False and "msg" in res, res)
    # a top-up fill with an amount still works (no --force needed past same-price dedupe: different price)
    res2, st2 = run(p, "--price", "62500", "--rmb", "1000")
    check("top-up fill with an amount ok (label falls back to the tier prefix)", res2.get("ok") is True and res2.get("label") == "B2", res2)


def t37_status_derive(tmp):
    """tier status derivation: partially filled -> partial / fully filled -> done"""
    p = fixture(tmp)
    res1, _ = run(p, "--price", "64000", "--label", "B2-a")
    check("one leg filled -> partial", res1["tier_status"] == "partial", res1)
    res2, _ = run(p, "--price", "61000", "--label", "B2-b")
    check("all legs filled -> done", res2["tier_status"] == "done", res2)


def t38_label_undo(tmp):
    """undo rollback: after a --label fill, undo -> leg status back to open, fill removed, totals rolled back"""
    p = fixture(tmp)
    before = json.loads(p.read_text("utf-8"))
    run(p, "--price", "61000", "--label", "B2-b")
    res, st = run(p, "--undo")
    check("undo ok", res.get("ok"), res)
    check("whole ledger restored field-by-field (incl. status back to open)", st == before, "diff")


# ---------------- label sets the tier (replacing price) + deviation soft check + --usdt ----------------

def t39_label_cross_tier(tmp):
    """label sets the tier, replacing price: price falls in B2's static range but label=C2-a -> fill lands in C2, track records C, B2 untouched"""
    p = fixture(tmp)
    res, st = run(p, "--price", "61000", "--label", "C2-a")
    check("ok and claimed C2 deep dip", res.get("ok") is True and res.get("claimed") == "C2 deep dip", res)
    b2, c2 = st["tiers"][0], st["tiers"][1]
    check("fill lands in C2.fills with price=61000", len(c2["fills"]) == 1 and c2["fills"][0]["price"] == 61000, c2["fills"])
    check("B2.fills unchanged", not b2["fills"], b2["fills"])
    check("track C deep-dip reserve +7000", st["tracks"][3]["used"] == 7000, st["tracks"])
    check("track B core dip-buy unchanged", st["tracks"][2]["used"] == 0, st["tracks"])
    check("amount taken from C2-a plan 7000", res.get("rmb") == 7000, res)
    check("C2-a status=filled", next(x for x in c2["plan"] if x["label"] == "C2-a")["status"] == "filled", c2["plan"])
    check("C2 tier wait->partial", c2["status"] == "partial", c2["status"])
    check("both B2 legs still open", all(x["status"] == "open" for x in b2["plan"]), b2["plan"])


def t40_label_price_in_gap(tmp):
    """label sets the tier: price falls in a gap between tiers (claim_tier misses) but with a label -> still records normally by label"""
    p = fixture(tmp)
    res, st = run(p, "--price", "55000", "--label", "C2-a")
    check("gap price + label -> ok", res.get("ok") is True, res)
    check("fill lands in C2", len(st["tiers"][1]["fills"]) == 1, st["tiers"][1]["fills"])


def t41_label_not_found_global(tmp):
    """label not found anywhere -> error lists the resting legs, no write"""
    p = fixture(tmp)
    before = p.read_text("utf-8")
    res, _ = run(p, "--price", "61000", "--label", "B9-z")
    check("ok=false and msg lists resting legs", res.get("ok") is False and "B2-a" in res.get("msg", ""), res)
    check("ledger unchanged", p.read_text("utf-8") == before)


def t42_label_price_deviation(tmp):
    """deviation soft check: fill price deviates >50% from the leg's trigger -> rejected; --force lets it through"""
    p = fixture(tmp)
    before = p.read_text("utf-8")
    res, _ = run(p, "--price", "590000", "--label", "B2-a")
    check("deviation >50% blocked and msg mentions deviation", res.get("ok") is False and "deviates" in res.get("msg", ""), res)
    check("ledger unchanged", p.read_text("utf-8") == before)
    res2, st2 = run(p, "--price", "590000", "--label", "B2-a", "--force")
    check("--force lets it through", res2.get("ok") is True, res2)
    check("after force, recorded at 590000", st2["tiers"][0]["fills"][0]["price"] == 590000, st2["tiers"][0]["fills"])


def t43_usdt_fill(tmp):
    """--usdt: converted to rmb by the ledger's rate (2000U x 7 = ¥14000)"""
    p = fixture(tmp)
    res, st = run(p, "--price", "64000", "--usdt", "2000")
    check("ok and rmb=14000", res.get("ok") is True and res.get("rmb") == 14000, res)
    check("invested +14000", st["invested_rmb"] == 64000, st["invested_rmb"])
    check("fill.usd consistent =2000", st["tiers"][0]["fills"][0]["usd"] == 2000, st["tiers"][0]["fills"])


def t44_usdt_mutex_and_bounds(tmp):
    """--usdt guards: mutually exclusive with --rmb / ≤0 / over RMB_MAX after conversion -> all blocked, no write"""
    p = fixture(tmp)
    before = p.read_text("utf-8")
    r1, _ = run(p, "--price", "64000", "--usdt", "2000", "--rmb", "14000")
    check("given with --rmb -> mutex block", r1.get("ok") is False and "mutually exclusive" in r1.get("msg", ""), r1)
    r2, _ = run(p, "--price", "64000", "--usdt", "-5")
    check("negative blocked", r2.get("ok") is False, r2)
    r3, _ = run(p, "--price", "64000", "--usdt", "30000")
    check("converted ¥210000 over RMB_MAX blocked", r3.get("ok") is False, r3)
    check("ledger unchanged in all cases", p.read_text("utf-8") == before)


def t45_usdt_over_budget(tmp):
    """--usdt triggers the over-budget guard after conversion: blocked, --force lets it through"""
    p = fixture(tmp, invested_rmb=195000, tracks=copy.deepcopy(BASE["tracks"]))
    res, st = run(p, "--price", "64000", "--usdt", "1000")  # 195000+7000>200000
    check("over budget blocked", res.get("ok") is False and "exceeds the budget" in res.get("msg", ""), res)
    check("ledger unchanged", st["invested_rmb"] == 195000, st["invested_rmb"])
    res2, _ = run(p, "--price", "64000", "--usdt", "1000", "--force")
    check("--force lets it through", res2.get("ok") is True, res2)


def t46_usdt_dca(tmp):
    """--dca --usdt: DCA also supports USDT denomination (500U x 7 = ¥3500)"""
    p = fixture(tmp)
    res, st = run(p, "--dca", "--usdt", "500")
    check("ok and rmb=3500", res.get("ok") is True and res.get("rmb") == 3500, res)
    check("DCA track 3000+3500", st["tracks"][1]["used"] == 6500, st["tracks"][1])


def t47_same_price_diff_label(tmp):
    """same-price different-leg in one tier: the second same-price leg is blocked but with a label-aware message (not falsely called a duplicate); --force records it to the second leg"""
    p = fixture(tmp)
    run(p, "--price", "51000", "--label", "C2-a")
    res, st = run(p, "--price", "51000", "--label", "C2-b")
    m = res.get("msg", "")
    check("blocked and message names both legs (C2-a recorded / this one C2-b)", res.get("ok") is False and "C2-a" in m and "C2-b" in m, res)
    check("not persisted (still only one entry)", len(st["tiers"][1]["fills"]) == 1, st["tiers"][1]["fills"])
    res2, st2 = run(p, "--price", "51000", "--label", "C2-b", "--force")
    check("--force lets it through", res2.get("ok") is True, res2)
    c2 = st2["tiers"][1]
    check("second entry recorded to C2-b (correct label)", any(f["label"] == "C2-b" for f in c2["fills"]) and len(c2["fills"]) == 2, c2["fills"])
    check("both legs filled", all(x["status"] == "filled" for x in c2["plan"]), c2["plan"])


def t48_label_backdate_undo(tmp):
    """label + --at backdate: fill.at=bare date; after --undo the whole ledger is restored field-by-field (leg back to open)"""
    p = fixture(tmp)
    before = json.loads(p.read_text("utf-8"))
    res, st = run(p, "--price", "51000", "--label", "C2-a", "--at", "2026-06-01")
    check("ok and fill.at=2026-06-01", res.get("ok") is True and st["tiers"][1]["fills"][0]["at"] == "2026-06-01", st["tiers"][1]["fills"])
    _, st2 = run(p, "--undo")
    check("undo restores the whole ledger", st2 == before, "diff")


def t49_old_path_exact_state(tmp):
    """legacy-path regression pin: after one fill without --label, key fields exactly match the existing behavior spec"""
    p = fixture(tmp)
    res, st = run(p, "--price", "63000")   # lands in B2, claims the closest leg B2-a@64000
    check("claims B2-a / rmb=14000", res.get("label") == "B2-a" and res.get("rmb") == 14000, res)
    check("invested=64000 / track B=14000 / total used=64000",
          st["invested_rmb"] == 64000 and st["tracks"][2]["used"] == 14000 and st["tracks"][0]["used"] == 64000, st["tracks"])
    check("held_btc=0.1+14000/7/63000", abs(st["held_btc"] - (0.1 + 14000 / 7 / 63000)) < 1e-9, st["held_btc"])
    check("B2-a filled / B2-b open / both C2 legs open untouched",
          st["tiers"][0]["plan"][0]["status"] == "filled" and st["tiers"][0]["plan"][1]["status"] == "open"
          and all(x["status"] == "open" for x in st["tiers"][1]["plan"]), st["tiers"])


CASES = [t01_fill_match_plan, t02_fill_to_done, t03_fill_explicit_rmb, t04_out_of_range,
         t05_price_invalid, t06_dca, t07_dca_no_amount, t08_rmb_invalid, t09_undo,
         t10_undo_empty, t11_double_report, t12_budget_cap, t13_corrupt_state,
         t14_missing_state, t15_recompute_avg, t16_no_temp_leftover, t17_append_only,
         t18_rate_zero, t19_emits_board,
         t20_extra_fund_basic, t21_extra_fund_no_source, t22_extra_fund_bad_amount,
         t23_extra_fund_over_200k, t24_extra_fund_undo, t25_concurrent_dca_locked,
         t26_backdate_fill, t27_backdate_extra_fund, t28_backdate_invalid,
         t29_backdate_dca, t30_no_at_keeps_timestamp, t31_dca_writes_last_at, t32_fill_not_touch_dca,
         t33_label_precise_claim, t34_label_whitelist, t35_no_label_only_open,
         t36_tier_full, t37_status_derive, t38_label_undo,
         t39_label_cross_tier, t40_label_price_in_gap, t41_label_not_found_global,
         t42_label_price_deviation, t43_usdt_fill, t44_usdt_mutex_and_bounds,
         t45_usdt_over_budget, t46_usdt_dca,
         t47_same_price_diff_label, t48_label_backdate_undo, t49_old_path_exact_state]


if __name__ == "__main__":
    for c in CASES:
        case(c)
    print(f"\n{'='*40}\nPassed {PASS} · Failed {FAIL}")
    sys.exit(1 if FAIL else 0)
