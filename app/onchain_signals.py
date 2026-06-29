"""
onchain_signals.py — On-chain signal data fetch + daily cache layer.

Data sources (all free, no API key required):
  · BRK / bitview.space   — STH cost basis, true market mean, realized price, RHODL, HODL waves,
                            365d profit/loss ratio, VDD raw material (coindays_destroyed × price
                            self-computed; BGeometrics free history only extends to 2022-06 and
                            cannot back-test bear-market bottoms, so it is excluded).
                            Additionally: 200WMA, realized_price (dip anchor, covered by R6 guard).
  · alternative.me /fng   — Fear & Greed Index

Design:
  - This file fetches only — it does not judge: raw readings are pulled and written to cache;
    signal evaluation is a pure function in strategy.py (single source of truth).
  - Cache is derived data, not the ledger: sole writer = daily cron running this file with --fetch
    (single writer, avoids multi-write contention); dashboard app.py is read-only.
    Completely separate from state.json — deployment and backup paths never overlap.
  - Graceful degradation: each source fetched independently; a single-source failure retains
    that source's last successful data (fetched_at unchanged), so the dashboard shows data age.
  - Atomic writes: tmp + os.replace, readers never see a partial JSON.
  - HTTPS certificate verification: urllib.request.urlopen verifies certificates by default;
    this file does NOT pass context=ssl.UNVERIFIED or set verify=False.
  - R6 anchor guard: each dip anchor has a sanity band [lo, hi] plus a max single-day jump rate;
    out-of-band values retain the old value and are flagged _suspect, so suspect prices are never
    passed to the strategy or display layer (guards against series mis-binding).
  - Green line (6m–10y RP): BRK has no exact age-band 6m–10y aggregated series (tested 2026-06-09,
    returns values outside the expected $59–60K range); set to null with source:manual, maintained
    manually by the user — never fabricated.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.request
from datetime import datetime, timedelta

HERE = pathlib.Path(__file__).parent


def _default_cache() -> str:
    """Default cache path: on the server the app (~/apps/btc-dashboard) and the cron (skill scripts)
    run in different directories, so HERE-relative paths would produce two separate files.
    When ~/.cockpit/btc-tracker/ exists (server deployment), converge to that directory (same
    directory as the ledger, but not the ledger itself — deployment/backup paths never overlap);
    local development falls back to the app directory."""
    server_dir = pathlib.Path.home() / ".cockpit" / "btc-tracker"
    if server_dir.is_dir():
        return str(server_dir / "onchain_cache.json")
    return str(HERE / "onchain_cache.json")


CACHE_PATH = pathlib.Path(os.environ.get("ONCHAIN_CACHE", _default_cache()))

BRK_BASE = "https://bitview.space/api/series"
FNG_URL = "https://api.alternative.me/fng/?limit=1"

# ── Halving cycle clock: next halving estimate ──────────────────────────────
# Next halving is pinned to block height 1,050,000 (210,000-block cycle;
# the 4th halving at block 840,000 landed 2024-04), estimated ~2028-04.
NEXT_HALVING_BLOCK = 1_050_000
AVG_BLOCK_MINUTES = 10   # Bitcoin target block interval (protocol difficulty adjustment anchor)
# Block-height data sources (all free, no API key; return plain integer text, parseable by json.loads).
# Primary: mempool.space (open-source self-hostable explorer, authoritative first-party) /
# fallback: blockstream.info (same format, used on degradation).
HEIGHT_SOURCES = [
    "https://mempool.space/api/blocks/tip/height",
    "https://blockstream.info/api/blocks/tip/height",
]
# R6-style sanity band (aligned with ANCHOR_GUARD philosophy in this module): heights outside
# this band are treated as dirty reads and rejected (prevents garbage heights from producing
# nonsensical est_date values). Lower bound is a safe value above the already-passed 4th
# halving at 840,000; upper bound is the target halving block.
HEIGHT_SANE_MIN, HEIGHT_SANE_MAX = 800_000, NEXT_HALVING_BLOCK

# VDD raw material history: multiple needs 365d MA + 30d MA + 60d low window + margin
VDD_HIST_DAYS = 500
# RHODL / P&L ratio history: 7d slope + display margin
HIST_DAYS = 30

# ── R6 anchor guard ───────────────────────────────────────────────────────
# Sanity band [lo, hi] (USD) for each dip anchor; out of band → retain old value + flag _suspect
# (prevents series mis-binding).
# Single-day jump threshold: max allowed change relative to the previous value (±20% = 0.20).
ANCHOR_GUARD: dict[str, tuple[float, float]] = {
    "wma200":         (20_000, 150_000),   # 200WMA: wide historical range, currently ~$61K
    "realized_price": (10_000, 150_000),   # On-chain realized price (C2 / double|enhanced anchor): currently ~$53.6K
}
ANCHOR_JUMP_THRESHOLD = 0.20  # Single-day jump >20% → flag as suspect


def _anchor_guard(key: str, new_val: float, old_val: float | None) -> tuple[float | None, bool]:
    """R6 guard: apply sanity band + single-day jump check to an anchor value.
    Returns (accepted_val, suspect):
      - suspect=False: new_val passes; returns new_val.
      - suspect=True : new_val is suspect; returns old_val (may be None).
    """
    lo, hi = ANCHOR_GUARD.get(key, (0.0, float("inf")))
    # Sanity band check
    if not (lo <= new_val <= hi):
        return old_val, True
    # Single-day jump check (only when a previous value is available)
    if old_val is not None and old_val != 0:
        change = abs(new_val - old_val) / abs(old_val)
        if change > ANCHOR_JUMP_THRESHOLD:
            return old_val, True
    return new_val, False


def _get_json(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={"User-Agent": "invest-cockpit/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _brk_latest(series: str, index: str = "1d") -> float:
    return float(_get_json(f"{BRK_BASE}/{series}/{index}/latest"))


def _brk_history(series: str, last_n: int, index: str = "1d") -> list[float]:
    rows = _get_json(f"{BRK_BASE}/{series}/{index}/data?from=-{last_n}")
    return [float(v) for v in rows]


# ── Three independent data sources (each can succeed or fail independently) ─
def fetch_brk(old_data: dict | None = None) -> dict:
    """BRK batch fetch. Any single series failure is treated as a whole-source failure
    (retain old cache), to avoid mixing stale and fresh readings in the same snapshot.

    Dip anchors (R6 guard): wma200 and realized_price are each checked for sanity band
    + single-day jump. Suspect values retain the old value and are flagged <key>_suspect: True.
    old_data is the data dict from the last successful fetch (for jump comparison); pass None
    on the first fetch.
    realized_price is the on-chain market realized price (not lth_realized_price);
    covered by the _apply_anchor guard (sanity band + single-day jump), never used raw.
    """
    seg_names = [
        "utxos_1d_to_1w_old_realized_cap", "utxos_1w_to_1m_old_realized_cap",
        "utxos_1m_to_2m_old_realized_cap", "utxos_2m_to_3m_old_realized_cap",
    ]
    segs = [_brk_latest(s) for s in seg_names]
    rcap = _brk_latest("realized_cap")

    result: dict = {
        "sth_realized_price": _brk_latest("sth_realized_price"),
        "true_market_mean": _brk_latest("true_market_mean"),     # ⚠️ Do not use cointime_price (different metric)
        "hodl_lt3m_pct": round(sum(segs) / rcap * 100, 2) if rcap else None,
        "rhodl_hist": _brk_history("rhodl_ratio", HIST_DAYS),
        # 365d P&L ratio: >1 means orange above blue; crossing 1.0 = orange-blue intersection
        # (signal ⑤); no need to self-compute 365d MA
        "pl_ratio_1y": _brk_latest("realized_profit_to_loss_ratio_1y"),
        "pl_ratio_1y_hist": _brk_history("realized_profit_to_loss_ratio_1y", HIST_DAYS),
        "pl_ratio_1w": _brk_latest("realized_profit_to_loss_ratio_1w"),  # Short-window reference (leading strength)
        # VDD raw material (signal ④): daily VDD = CDD × close price; multiple computed in strategy
        "cdd_hist": _brk_history("coindays_destroyed_sum_24h", VDD_HIST_DAYS),
        "price_close_hist": _brk_history("price_close", VDD_HIST_DAYS),
    }

    # ── Dip anchors (R6 guard) ────────────────────────────────────────────
    # Green line (6m–10y RP): BRK has no exact age-band aggregated series → set to null
    # (never fabricate a price)
    result["green_line"] = None
    result["green_line_source"] = "manual"

    # 200WMA (price_sma_200w) → heavier|double tier boundary + C1
    _apply_anchor(result, "wma200", _brk_latest("price_sma_200w"), old_data)

    # On-chain realized price → double|enhanced tier boundary + C2.
    # Covered by guard — never used raw.
    _apply_anchor(result, "realized_price", _brk_latest("realized_price"), old_data)

    return result


def _apply_anchor(result: dict, key: str, new_val: float, old_data: dict | None) -> None:
    """Write an anchor value through the R6 guard into result; flag <key>_suspect=True and
    retain the old value if the new value is suspect."""
    old_val = (old_data or {}).get(key)
    accepted, suspect = _anchor_guard(key, new_val, old_val)
    result[key] = accepted
    if suspect:
        result[f"{key}_suspect"] = True


def fetch_fng() -> dict:
    j = _get_json(FNG_URL)
    item = j["data"][0]
    return {"value": int(item["value"]), "label": str(item.get("value_classification", ""))}


# ── Cycle clock: halving estimate ─────────────────────────────────────────
def estimate_halving(height: int, now: datetime) -> dict:
    """Pure function: estimate the next halving date from the current block height.
    height and now are injectable (for deterministic unit testing).
    est_date = now + remaining_blocks × 10 minutes, frozen to a calendar date
    (snapshot taken at fetch time, so the value is stable throughout the week and
    does not drift with the display time). height ≥ target → remaining=0, est_date=today."""
    remaining = max(0, NEXT_HALVING_BLOCK - height)
    est = now + timedelta(minutes=remaining * AVG_BLOCK_MINUTES)
    return {"height": height, "halving_block": NEXT_HALVING_BLOCK,
            "blocks_remaining": remaining, "est_date": est.date().isoformat()}


def _fetch_block_height() -> int:
    """Fetch the current block height, with R6 sanity band (heights outside the band are
    treated as dirty reads and skipped). Raises if all sources fail or produce dirty reads
    (caught by refresh's per-source degradation, which retains the old cache entry)."""
    last_err: Exception | None = None
    for url in HEIGHT_SOURCES:
        try:
            h = int(_get_json(url))
            if not (HEIGHT_SANE_MIN <= h <= HEIGHT_SANE_MAX):
                raise ValueError(f"height {h} out of sanity band [{HEIGHT_SANE_MIN},{HEIGHT_SANE_MAX}]")
            return h
        except Exception as e:  # noqa: BLE001 — single-source error / dirty read → try next source
            last_err = e
    raise last_err or RuntimeError("no block height source available")


def fetch_cycle(now: datetime | None = None) -> dict:
    """Cycle clock reading: fetch height → estimate halving. now is injectable for tests;
    production calls use the current time."""
    now = now or datetime.now().astimezone()
    return estimate_halving(_fetch_block_height(), now)


SOURCES = {"brk": fetch_brk, "fng": fetch_fng, "cycle": fetch_cycle}


# ── Cache read/write ──────────────────────────────────────────────────────
def load_cache(path: pathlib.Path = None) -> dict:
    p = path or CACHE_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict, path: pathlib.Path):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def refresh(now: datetime | None = None, path: pathlib.Path = None) -> dict:
    """Fetch all sources and merge into the cache file. Single-source failure → retain that
    source's last successful data/fetched_at and mark ok:False. Returns the merged cache dict.
    now is injectable for tests."""
    path = path or CACHE_PATH
    now = now or datetime.now().astimezone()
    # Retain only segments for current SOURCES: prevents zombie keys from retired sources
    cache = {k: v for k, v in load_cache(path).items() if k in SOURCES}
    for name, fn in SOURCES.items():
        try:
            # For brk: pass old data so the R6 single-day jump guard can compare against it
            old_entry = cache.get(name) or {}
            if name == "brk":
                data = fn(old_data=old_entry.get("data"))
            elif name == "cycle":
                data = fn(now=now)   # Pass refresh's now through so est_date baseline matches fetched_at
            else:
                data = fn()
            cache[name] = {"ok": True, "fetched_at": now.isoformat(timespec="seconds"), "data": data}
        except Exception as e:  # noqa: BLE001 — source errors always degrade gracefully so cron stays green
            old = cache.get(name) or {}
            cache[name] = {"ok": False, "fetched_at": old.get("fetched_at", ""),
                           "data": old.get("data"), "error": f"{type(e).__name__}: {e}"[:160]}
    _save_cache(cache, path)
    return cache


if __name__ == "__main__":
    if "--fetch" in sys.argv:
        c = refresh()
        bad = [k for k, v in c.items() if not v.get("ok")]
        print(json.dumps({"ok": not bad, "failed": bad,
                          "fetched_at": {k: v.get("fetched_at", "") for k, v in c.items()}},
                         ensure_ascii=False))
        sys.exit(0)
    if "--show" in sys.argv:
        print(json.dumps(load_cache(), ensure_ascii=False, indent=2)[:4000])
        sys.exit(0)
    print("Usage: onchain_signals.py --fetch | --show")
