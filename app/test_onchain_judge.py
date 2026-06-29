"""on-chain signal pure-function unit tests (full triggered / not-triggered / boundary coverage)."""

from datetime import datetime

import strategy as S

NOW = datetime.fromisoformat("2026-06-06T10:00:00+10:00")
PASS = 0


def ok(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ✓ {name}")


def t_vdd_math():
    print("· VDD Multiple (30d/365d) math")
    ok("insufficient history -> empty", S.vdd_multiple_series([1.0] * S.VDD_MA_LONG, [60000.0] * S.VDD_MA_LONG) == [])
    # constant CDD x constant price -> multiple stays 1
    n = S.VDD_MA_LONG + 3
    ms = S.vdd_multiple_series([5.0] * n, [60000.0] * n)
    ok("constant series multiple=1", len(ms) == 3 and all(abs(m - 1) < 1e-9 for m in ms))
    # last 30 days CDD x5 -> multiple = 5 / ((335+150)/365) ~= 3.76
    cdd = [1.0] * (500 - 30) + [5.0] * 30
    ms2 = S.vdd_multiple_series(cdd, [60000.0] * 500)
    ok("pulse segment multiple~=3.76", abs(ms2[-1] - 5 / (485 / 365)) < 1e-9)
    ok("uneven series lengths align on the tail", len(S.vdd_multiple_series([1.0] * 400, [60000.0] * 500)) == 400 - S.VDD_MA_LONG)


def t_sig_sth():
    print("· (1) STH/TMM inversion")
    hit = S.sig_sth_inversion({"sth_realized_price": 74800.0, "true_market_mean": 77400.0})
    ok("inverted -> hit/green", hit["status"] == "hit" and hit["sem"] == "green" and hit["frac"] == 1.0)
    ok("sub carries both line readings", "74.8K" in hit["sub"] and "77.4K" in hit["sub"])
    wait = S.sig_sth_inversion({"sth_realized_price": 81502.0, "true_market_mean": 78622.0})
    ok("not inverted -> wait", wait["status"] == "wait" and 0 < wait["frac"] < 1)
    ok("missing data -> na", S.sig_sth_inversion({})["status"] == "na")


def t_sig_hodl():
    print("· (2) STH share ice-point")
    ok("15.3% -> hit", S.sig_hodl_ice({"hodl_lt3m_pct": 15.3})["status"] == "hit")
    ok("20% -> warn (within reference band)", S.sig_hodl_ice({"hodl_lt3m_pct": 20.0})["status"] == "warn")
    ok("30% -> wait", S.sig_hodl_ice({"hodl_lt3m_pct": 30.0})["status"] == "wait")
    ok("18.8 boundary -> warn (strictly-less-than for hit)", S.sig_hodl_ice({"hodl_lt3m_pct": 18.8})["status"] == "warn")


def t_sig_rhodl():
    print("· (3) RHODL price-volume divergence")
    up = list(range(30))            # price rising
    down = list(range(30, 0, -1))   # RHODL falling
    d = S.sig_rhodl_divergence({"rhodl_hist": down}, up)
    ok("price up / volume down -> warn divergence", d["status"] == "warn" and "price ↑" in d["sub"] and "RHODL ↓" in d["sub"])
    ok("same direction -> wait", S.sig_rhodl_divergence({"rhodl_hist": up}, up)["status"] == "wait")
    ok("price falling -> wait (divergence only warns on the rally leg)", S.sig_rhodl_divergence({"rhodl_hist": down}, down)["status"] == "wait")
    ok("insufficient data -> na", S.sig_rhodl_divergence({"rhodl_hist": [1, 2]}, up)["status"] == "na")


def t_sig_vdd():
    print("· (4) VDD pulse (backtest-calibrated rule: bear state + water level + 2x)")
    bear_px = [60000.0] * 499 + [59999.0]   # last point slightly below the 365d MA -> bear state
    bull_px = [60000.0] * 500               # equal to the MA -> not bear state
    pulse_cdd = [1.0] * 470 + [5.0] * 30    # mult~=3.76, jump~=3.76
    mild_cdd = [1.0] * 470 + [1.5] * 30     # mult~=1.44, jump~=1.44 (below 2x)
    flat_cdd = [1.0] * 500                  # mult=1
    hit = S.sig_vdd_pulse({"cdd_hist": pulse_cdd, "price_close_hist": bear_px})
    ok("bear + pulse -> hit", hit["status"] == "hit" and "Multiple" in hit["sub"])
    ok("bull + same pulse -> wait (not bear state = not capitulation)",
       S.sig_vdd_pulse({"cdd_hist": pulse_cdd, "price_close_hist": bull_px})["status"] == "wait")
    ok("bear + mild rise -> warn building",
       S.sig_vdd_pulse({"cdd_hist": mild_cdd, "price_close_hist": bear_px})["status"] == "warn")
    ok("bear + flat -> wait",
       S.sig_vdd_pulse({"cdd_hist": flat_cdd, "price_close_hist": bear_px})["status"] == "wait")
    ok("insufficient history -> na", S.sig_vdd_pulse({"cdd_hist": [1.0] * 100, "price_close_hist": [6e4] * 100})["status"] == "na")
    ok("empty input -> na", S.sig_vdd_pulse({})["status"] == "na")


def t_sig_pl():
    print("· (5) P/L 365d crossover")
    ok("0.98 -> hit crossover", S.sig_pl_cross({"pl_ratio_1y": 0.98})["status"] == "hit")
    ok("1.0 boundary -> hit (<=1 is a crossover)", S.sig_pl_cross({"pl_ratio_1y": 1.0})["status"] == "hit")
    near = S.sig_pl_cross({"pl_ratio_1y": 1.1, "pl_ratio_1y_hist": [1.5] * 6 + [1.1]})
    ok("1.10 -> warn approaching + converging note", near["status"] == "warn" and "converging" in near["note"])
    ok("1.66 -> wait", S.sig_pl_cross({"pl_ratio_1y": 1.66})["status"] == "wait")
    ok("missing data -> na", S.sig_pl_cross({})["status"] == "na")


def t_sig_fng():
    print("· (8) Fear & Greed automation")
    ok("5 -> hit discretionary-reserve alert", S.sig_fng({"value": 5, "label": "Extreme Fear"})["status"] == "hit")
    ok("12 -> warn", S.sig_fng({"value": 12, "label": "Extreme Fear"})["status"] == "warn")
    ok("55 -> wait", S.sig_fng({"value": 55, "label": "Greed"})["status"] == "wait")
    ok("empty -> na", S.sig_fng({})["status"] == "na")


def t_staleness():
    print("· cache staleness annotation")
    ok("within 12h -> no annotation", S._staleness("2026-06-06T00:00:00+10:00", NOW) == "")
    ok("40h -> hours", S._staleness("2026-06-04T18:00:00+10:00", NOW) == "data 40h old")
    ok("3 days -> days", S._staleness("2026-06-03T08:00:00+10:00", NOW) == "data 3d old")
    ok("parse failure -> unknown", S._staleness("garbage", NOW) == "data timestamp unknown")


def _cache(fng_ok=True, stale=False):
    at = "2026-06-03T08:00:00+10:00" if stale else "2026-06-06T08:00:00+10:00"
    c = {"brk": {"ok": True, "fetched_at": at, "data": {
            "sth_realized_price": 74800.0, "true_market_mean": 77400.0, "realized_price": 53646.0,
            "hodl_lt3m_pct": 15.3, "rhodl_hist": list(range(30, 0, -1)),
            "pl_ratio_1y": 1.66, "pl_ratio_1y_hist": [1.7] * 30, "pl_ratio_1w": 0.125,
            "cdd_hist": [1.0] * 500, "price_close_hist": [60000.0] * 500}},
         "fng": {"ok": True, "fetched_at": at, "data": {"value": 12, "label": "Extreme Fear"}}}
    if not fng_ok:
        c["fng"]["data"] = None
    return c


MANUAL = [{"name": "Triple bottom", "frac": 0.67, "note": "second prong done", "sem": "green"},
          {"name": "4-year cycle", "frac": 0.94, "note": "0.94 / 1.0", "sem": "amber"},
          {"name": "Fear & Greed", "frac": 0.26, "note": "26 extreme fear", "sem": "red"}]


def t_cards_assembly():
    print("· signal-board assembly")
    cards = S.onchain_signal_cards(_cache(), list(range(30)), MANUAL, NOW)
    keys = [c.get("key") for c in cards if c.get("auto")]
    ok("six auto cards", keys == ["sth_tmm", "hodl_ice", "rhodl_div", "vdd", "pl_cross", "fng"])
    names = [c["name"] for c in cards if not c.get("auto")]
    ok("manual cards keep triple-bottom/4-year, F&G auto-replaced", names == ["Triple bottom", "4-year cycle"])
    ok("(1) already inverted -> hit", cards[0]["status"] == "hit")
    ok("legacy front-end fields intact (name/frac/note/sem all present)",
       all(set(("name", "frac", "note", "sem")) <= set(c) for c in cards))
    # auto F&G has no data -> manual F&G fallback is kept
    cards2 = S.onchain_signal_cards(_cache(fng_ok=False), list(range(30)), MANUAL, NOW)
    ok("auto F&G na -> manual F&G fallback", any(c["name"] == "Fear & Greed" and not c.get("auto") for c in cards2))
    # stale cache -> sub carries the staleness annotation
    cards3 = S.onchain_signal_cards(_cache(stale=True), list(range(30)), MANUAL, NOW)
    ok("stale cache annotated 'data 3d old'", "data 3d old" in cards3[0]["sub"])
    # empty cache -> all na, no crash
    cards4 = S.onchain_signal_cards({}, [], MANUAL, NOW)
    ok("empty cache -> auto cards all na + manual fallback", all(c["status"] == "na" for c in cards4 if c.get("auto"))
       and any(c["name"] == "Fear & Greed" for c in cards4))


def t_resonance():
    print("· bear-bottom dual-confirmation resonance")
    mk = lambda v, p: [{"key": "vdd", "status": v}, {"key": "pl_cross", "status": p}]
    ok("(4)+(5) both hit -> gold badge", S.onchain_resonance(mk("hit", "hit"))["active"] is True)
    ok("(5) only -> off", S.onchain_resonance(mk("wait", "hit"))["active"] is False)
    ok("(4) only -> off", S.onchain_resonance(mk("hit", "warn"))["active"] is False)
    ok("empty list -> no crash", S.onchain_resonance([])["active"] is False)


if __name__ == "__main__":
    t_vdd_math()
    t_sig_sth()
    t_sig_hodl()
    t_sig_rhodl()
    t_sig_vdd()
    t_sig_pl()
    t_sig_fng()
    t_staleness()
    t_cards_assembly()
    t_resonance()
    print(f"✓ on-chain signal judgement: all {PASS} assertions passed")
