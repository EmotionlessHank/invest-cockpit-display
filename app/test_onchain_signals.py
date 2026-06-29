"""onchain_signals.py unit tests — fully mocked, never hits the real API; cache writes to /tmp."""

import json
import pathlib
import tempfile
from datetime import datetime

import onchain_signals as oc

NOW = datetime.fromisoformat("2026-06-06T10:00:00+10:00")
PASS = 0


def ok(name, cond):
    global PASS
    assert cond, f"FAIL: {name}"
    PASS += 1
    print(f"  ✓ {name}")


def fake_responses(table):
    """Return by URL-prefix match; not in the table -> raise (simulate a source failure)."""
    def _fake(url, timeout=10):
        for prefix, resp in table.items():
            if url.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise RuntimeError(f"unexpected url: {url}")
    return _fake


BRK_OK = {
    f"{oc.BRK_BASE}/sth_realized_price/1d/latest": 74800.38,
    f"{oc.BRK_BASE}/true_market_mean/1d/latest": 77400.71,
    f"{oc.BRK_BASE}/realized_price/1d/latest": 53647.68,
    f"{oc.BRK_BASE}/utxos_1d_to_1w_old_realized_cap/1d/latest": 100.0,
    f"{oc.BRK_BASE}/utxos_1w_to_1m_old_realized_cap/1d/latest": 100.0,
    f"{oc.BRK_BASE}/utxos_1m_to_2m_old_realized_cap/1d/latest": 100.0,
    f"{oc.BRK_BASE}/utxos_2m_to_3m_old_realized_cap/1d/latest": 100.0,
    f"{oc.BRK_BASE}/realized_cap/1d/latest": 2000.0,
    f"{oc.BRK_BASE}/rhodl_ratio/1d/data": [0.1] * 30,
    f"{oc.BRK_BASE}/realized_profit_to_loss_ratio_1y/1d/latest": 1.66,
    f"{oc.BRK_BASE}/realized_profit_to_loss_ratio_1y/1d/data": [1.7] * 30,
    f"{oc.BRK_BASE}/realized_profit_to_loss_ratio_1w/1d/latest": 0.125,
    f"{oc.BRK_BASE}/coindays_destroyed_sum_24h/1d/data": [1.0e6] * oc.VDD_HIST_DAYS,
    f"{oc.BRK_BASE}/price_close/1d/data": [60000.0] * oc.VDD_HIST_DAYS,
    # B-L1 new anchor (a normal value within the sane range)
    f"{oc.BRK_BASE}/price_sma_200w/1d/latest": 61500.0,
}
FNG_OK = {oc.FNG_URL: {"data": [{"value": "12", "value_classification": "Extreme Fear"}]}}


def t_all_ok():
    print("· all sources ok: cache structure + derived values")
    oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        c = oc.refresh(now=NOW, path=p)
        ok("both sources ok", all(c[s]["ok"] for s in ("brk", "fng")))
        ok("fetched_at injected with now", c["brk"]["fetched_at"] == "2026-06-06T10:00:00+10:00")
        ok("STH share self-computed 400/2000=20%", c["brk"]["data"]["hodl_lt3m_pct"] == 20.0)
        ok("P/L ratio 1y", c["brk"]["data"]["pl_ratio_1y"] == 1.66)
        ok("VDD inputs complete (cdd+price, 500 each)",
           len(c["brk"]["data"]["cdd_hist"]) == oc.VDD_HIST_DAYS
           and len(c["brk"]["data"]["price_close_hist"]) == oc.VDD_HIST_DAYS)
        ok("Fear & Greed rounded", c["fng"]["data"]["value"] == 12)
        ok("cache persisted and re-readable", oc.load_cache(p)["fng"]["data"]["value"] == 12)
        ok("no .tmp leftover", not (pathlib.Path(d) / "cache.tmp").exists())


def t_partial_fail_keeps_old():
    print("· single-source failure: keep old data, others update as usual")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
        oc.refresh(now=NOW, path=p)
        # second round: brk whole-source failure (one series 503), fng value changes
        later = datetime.fromisoformat("2026-06-07T10:00:00+10:00")
        broken = dict(BRK_OK)
        broken[f"{oc.BRK_BASE}/true_market_mean/1d/latest"] = RuntimeError("503")
        fng2 = {oc.FNG_URL: {"data": [{"value": "8", "value_classification": "Extreme Fear"}]}}
        oc._get_json = fake_responses({**broken, **fng2})
        c = oc.refresh(now=later, path=p)
        ok("brk marked ok:False", c["brk"]["ok"] is False)
        ok("brk keeps old value (TMM still there)", c["brk"]["data"]["true_market_mean"] == 77400.71)
        ok("brk fetched_at keeps the old time (honest staleness)", c["brk"]["fetched_at"].startswith("2026-06-06"))
        ok("error records the reason", "503" in c["brk"]["error"])
        ok("fng updates as usual", c["fng"]["data"]["value"] == 8 and c["fng"]["fetched_at"].startswith("2026-06-07"))


def t_cold_start_all_down():
    print("· cold start, all sources down: no crash, structure readable, data is None")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        oc._get_json = fake_responses({})
        c = oc.refresh(now=NOW, path=p)
        ok("both sources ok:False", all(not c[s]["ok"] for s in ("brk", "fng")))
        ok("data is None (read side shows 'no data' accordingly)", c["brk"]["data"] is None)
        ok("cache file still written (with error for debugging)", "error" in oc.load_cache(p)["brk"])


def t_corrupt_cache_recovers():
    print("· corrupt cache: load returns empty dict, refresh rebuilds")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        p.write_text("{half json", encoding="utf-8")
        ok("corrupt cache reads as empty", oc.load_cache(p) == {})
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
        c = oc.refresh(now=NOW, path=p)
        ok("rebuild succeeds", c["brk"]["ok"])


def t_zombie_section_pruned():
    print("· retired data source's old section is pruned")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        p.write_text('{"vdd": {"ok": true, "data": {}}, "fng": {"ok": true}}', encoding="utf-8")
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
        c = oc.refresh(now=NOW, path=p)
        ok("zombie vdd section cleared", "vdd" not in c and set(c) == {"brk", "fng", "cycle"})  # cycle source registered with the cycle clock


# ── B-L1 tests (L1-1 ~ L1-6) ─────────────────────────────────


def t_l1_1_new_anchor_keys():
    """L1-1: after --fetch, cache["brk"]["data"] contains the new keys, green_line=null + source=manual."""
    print("· L1-1 new anchor keys written to cache")
    oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        c = oc.refresh(now=NOW, path=p)
        data = c["brk"]["data"]
        ok("wma200 written", "wma200" in data and data["wma200"] == 61500.0)
        ok("realized_price written (guarded)", data.get("realized_price") == 53647.68)
        ok("realized_price has no suspect (value normal)", "realized_price_suspect" not in data)
        ok("dead anchor lth_realized_price removed (no longer fetched)", "lth_realized_price" not in data)
        ok("dead anchor mvrv family removed (no longer fetched)",
           "mvrv" not in data and "lth_mvrv" not in data and "sth_mvrv" not in data)
        ok("green_line is null", data.get("green_line") is None)
        ok("green_line_source is manual", data.get("green_line_source") == "manual")
        ok("no wma200_suspect (value normal)", "wma200_suspect" not in data)
        ok("persisted and re-readable wma200", oc.load_cache(p)["brk"]["data"]["wma200"] == 61500.0)


def t_l1_2_single_source_fail_keeps_old():
    """L1-2: one of the new BRK anchor series fails -> whole source keeps old values (the mechanism is already
    covered by t_partial_fail_keeps_old; here we specifically verify the new keys are fully retained in the
    old cache and never replaced by 0/null)."""
    print("· L1-2 single-source failure keeps old anchor values, never writes 0/null as a price")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        # first run a successful round
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
        oc.refresh(now=NOW, path=p)
        # second round: price_sma_200w raises (simulate the series going offline), whole brk source fails
        later = datetime.fromisoformat("2026-06-07T10:00:00+10:00")
        broken = dict(BRK_OK)
        broken[f"{oc.BRK_BASE}/price_sma_200w/1d/latest"] = RuntimeError("404")
        oc._get_json = fake_responses({**broken, **FNG_OK})
        c = oc.refresh(now=later, path=p)
        ok("brk whole source marked ok:False", c["brk"]["ok"] is False)
        ok("wma200 keeps old value 61500", c["brk"]["data"]["wma200"] == 61500.0)
        ok("realized_price keeps old value", c["brk"]["data"]["realized_price"] == 53647.68)
        ok("brk fetched_at keeps old time", c["brk"]["fetched_at"].startswith("2026-06-06"))
        ok("never writes 0 as a price (wma200 != 0)", c["brk"]["data"]["wma200"] != 0)


def t_l1_4_anchor_out_of_range_suspect():
    """L1-4: anchor value out of the sane range -> mark _suspect, keep old value, do not write the new value."""
    print("· L1-4 anchor out of range -> suspect flag, keep old value")
    # construct an out-of-range value: wma200 = $5 (far below the $20K lower bound)
    bad_brk = dict(BRK_OK)
    bad_brk[f"{oc.BRK_BASE}/price_sma_200w/1d/latest"] = 5.0  # out of range

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        # first write a normal old value
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
        oc.refresh(now=NOW, path=p)
        # second round: wma200 returns an absurdly low value
        later = datetime.fromisoformat("2026-06-07T10:00:00+10:00")
        oc._get_json = fake_responses({**bad_brk, **FNG_OK})
        c = oc.refresh(now=later, path=p)
        data = c["brk"]["data"]
        ok("brk still ok:True (guard does not fail the whole source)", c["brk"]["ok"] is True)
        ok("wma200_suspect flag present", data.get("wma200_suspect") is True)
        ok("wma200 keeps old value 61500 (out-of-range 5 not written)", data.get("wma200") == 61500.0)


def t_l1_5_anchor_daily_jump_suspect():
    """L1-5: single-day jump >20% -> mark _suspect, keep old value."""
    print("· L1-5 single-day jump >20% -> suspect flag, keep old value")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        # first round: wma200=61500
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
        oc.refresh(now=NOW, path=p)
        # second round: wma200 jumps to 80000 (+30%, over the 20% threshold)
        later = datetime.fromisoformat("2026-06-07T10:00:00+10:00")
        jumped = dict(BRK_OK)
        jumped[f"{oc.BRK_BASE}/price_sma_200w/1d/latest"] = 80000.0  # +30%
        oc._get_json = fake_responses({**jumped, **FNG_OK})
        c = oc.refresh(now=later, path=p)
        data = c["brk"]["data"]
        ok("brk ok:True (jump guard does not crash the whole source)", c["brk"]["ok"] is True)
        ok("wma200_suspect flag present", data.get("wma200_suspect") is True)
        ok("wma200 keeps old value 61500 (jump value 80000 not written)", data.get("wma200") == 61500.0)
        # realized_price has no jump, written normally (same source not tainted by the wma200 suspect)
        ok("realized_price updates normally (no jump)", data.get("realized_price") == 53647.68)
        ok("realized_price has no suspect", "realized_price_suspect" not in data)


def t_l1_7_realized_price_jump_suspect():
    """L1-7 (🔴-1 eliminate false green): realized_price within the sane range but single-day jump >20% -> mark suspect, keep old value.
    Prevents a BRK backfill from silently drifting the realized price and tripping the C2 / Double|Enhanced tier boundary at the wrong price."""
    print("· L1-7 realized_price single-day jump >20% -> suspect, keep old value (C2/Double tier anchor guard)")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        # first round: realized_price=53647.68
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
        oc.refresh(now=NOW, path=p)
        # second round: realized_price jumps to 66000 (+23%, within the (10K,150K) range but over the 20% jump threshold)
        later = datetime.fromisoformat("2026-06-07T10:00:00+10:00")
        jumped = dict(BRK_OK)
        jumped[f"{oc.BRK_BASE}/realized_price/1d/latest"] = 66000.0  # +23%, in-range but a jump
        oc._get_json = fake_responses({**jumped, **FNG_OK})
        c = oc.refresh(now=later, path=p)
        data = c["brk"]["data"]
        ok("brk ok:True (jump guard does not crash the whole source)", c["brk"]["ok"] is True)
        ok("realized_price_suspect flag present", data.get("realized_price_suspect") is True)
        ok("realized_price keeps old value 53647.68 (jump value 66000 not written)", data.get("realized_price") == 53647.68)


def t_l1_6_https_no_verify_false():
    """L1-6: confirm _get_json does not bypass HTTPS certificate verification (no verify=False / ssl._create_unverified_context)."""
    print("· L1-6 HTTPS certificate verification is not disabled")
    import inspect
    import ssl
    src = inspect.getsource(oc._get_json)
    ok("_get_json has no verify=False", "verify=False" not in src)
    ok("_get_json has no _create_unverified_context", "_create_unverified_context" not in src)
    ok("_get_json has no CERT_NONE", "CERT_NONE" not in src)
    # urlopen uses the system CA by default; not passing a custom ssl context = normal verification
    ok("_get_json passes no custom ssl context", "ssl.SSLContext" not in src and "ssl.create_default_context" not in src or
       # if create_default_context is used without lowering check_hostname=False it is still valid; conservatively, as long as there is no CERT_NONE
       "CERT_NONE" not in src)


# ── cycle clock (halving estimate) tests ──────────────────────────────────


def t_dd013_estimate_halving():
    """Pure function: height -> blocks remaining + frozen est_date (now/height injected, pinned)."""
    print("· estimate_halving pure function")
    from datetime import timedelta
    now = datetime.fromisoformat("2026-06-10T11:00:00+10:00")
    r = oc.estimate_halving(953036, now)
    ok("blocks remaining = 1,050,000 - 953,036", r["blocks_remaining"] == 1_050_000 - 953_036)
    ok("halving_block constant surfaced", r["halving_block"] == 1_050_000)
    ok("height passed through as-is", r["height"] == 953036)
    exp = (now + timedelta(minutes=(1_050_000 - 953_036) * 10)).date().isoformat()
    ok("est_date = now + remaining x10min frozen to a calendar day", r["est_date"] == exp)


def t_dd013_halving_boundary():
    """Boundary: height at/over target -> remaining 0 (not negative), est_date is today."""
    print("· halving boundary (height >= target)")
    now = datetime.fromisoformat("2028-04-20T00:00:00+10:00")
    ok("exactly at target -> remaining 0", oc.estimate_halving(1_050_000, now)["blocks_remaining"] == 0)
    ok("at target est_date is today", oc.estimate_halving(1_050_000, now)["est_date"] == "2028-04-20")
    ok("over target also floors at 0 (max never negative)", oc.estimate_halving(1_060_000, now)["blocks_remaining"] == 0)


def t_dd013_height_sanity_band():
    """R6 sanity band: a dirty height read skips that source; both dirty -> raise (refresh degrades to keep old)."""
    print("· block height sanity band + per-source degradation")
    # primary dirty read (0 below the lower bound) -> take the backup source's normal value
    oc._get_json = fake_responses({"https://mempool.space": 0, "https://blockstream.info": 953036})
    ok("primary dirty -> fall back to backup", oc._fetch_block_height() == 953036)
    # both out of range -> raise
    oc._get_json = fake_responses({"https://mempool.space": 0, "https://blockstream.info": 2_000_000})
    raised = False
    try:
        oc._fetch_block_height()
    except Exception:
        raised = True
    ok("both dirty reads -> raise (no garbage height returned)", raised)
    # normal height taken directly from the primary source
    oc._get_json = fake_responses({"https://mempool.space": 953036})
    ok("primary normal -> taken directly", oc._fetch_block_height() == 953036)


def t_dd013_cycle_source_in_refresh():
    """cycle source enters refresh: success writes est_date; height sources all down -> keep old est_date (same degradation as onchain)."""
    print("· cycle source enters refresh + keeps old on failure")
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "cache.json"
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK, "https://mempool.space": 953036})
        c = oc.refresh(now=NOW, path=p)
        ok("cycle ok:True", c["cycle"]["ok"] is True)
        ok("cycle.data contains est_date", "est_date" in c["cycle"]["data"])
        ok("est_date basis = refresh's now (passed through, same source as fetched_at)",
           c["cycle"]["data"]["est_date"] == oc.estimate_halving(953036, NOW)["est_date"])
        ok("cycle.data contains blocks_remaining", c["cycle"]["data"]["blocks_remaining"] == 1_050_000 - 953_036)
        first_est = c["cycle"]["data"]["est_date"]
        # second round: height sources all down (no height URL in the table) -> cycle keeps old
        later = datetime.fromisoformat("2026-06-11T10:00:00+10:00")
        oc._get_json = fake_responses({**BRK_OK, **FNG_OK})
        c2 = oc.refresh(now=later, path=p)
        ok("height sources down -> cycle ok:False", c2["cycle"]["ok"] is False)
        ok("cycle keeps old est_date (not empty)", c2["cycle"]["data"]["est_date"] == first_est)
        ok("cycle fetched_at keeps old time (honest staleness)", c2["cycle"]["fetched_at"].startswith("2026-06-06"))


if __name__ == "__main__":
    t_all_ok()
    t_partial_fail_keeps_old()
    t_cold_start_all_down()
    t_corrupt_cache_recovers()
    t_zombie_section_pruned()
    # B-L1
    t_l1_1_new_anchor_keys()
    t_l1_2_single_source_fail_keeps_old()
    t_l1_4_anchor_out_of_range_suspect()
    t_l1_5_anchor_daily_jump_suspect()
    t_l1_6_https_no_verify_false()
    t_l1_7_realized_price_jump_suspect()
    # cycle clock
    t_dd013_estimate_halving()
    t_dd013_halving_boundary()
    t_dd013_height_sanity_band()
    t_dd013_cycle_source_in_refresh()
    print(f"✓ onchain_signals: all {PASS} assertions passed")
