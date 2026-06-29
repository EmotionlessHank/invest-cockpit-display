---
name: mstr-tracker
description: "MSTR (MicroStrategy stock) buy-the-dip progress tracking and bookkeeping. [When to use this skill] The user asks about MSTR progress / investment progress / dip-buying progress / holdings / how much is left / mNAV / 'how is MSTR doing now' / 'open the dashboard' → run the scripts to produce the progress board (with live MSTR price + mNAV) + a dashboard link; the user verbally reports a fill (e.g. 'T2 filled at 91, bought 30 shares', 'T1 filled at 130') or wants to undo → record it, claim the round, update progress. **For anything involving MSTR dip-buying / holdings / investment progress, always run the scripts via this skill; never answer from memory or make up numbers (especially the price/mNAV).** Read-only monitoring, zero order placement."
version: 1.0.0
author: Hank
triggers: ["/mstr", "/MSTR", "mstr progress", "MSTR holdings", "mnav"]
platforms: [linux]
metadata:
  agent:
    tags: [mstr, microstrategy, crypto, tracker, mnav]
---

# MSTR Dip Tracking — Conversational Bookkeeping + Live Dashboard

The user manually places MSTR limit orders (GTC) at their broker to buy the dip. This skill lets them report in plain language while you automatically record the entry and provide a dashboard link. **Read-only monitoring; never place orders.** MSTR is tracked by **share count**, denominated in USD.

## 🔴 Hard rules (violating them produces nonsense answers)

1. **Any question about MSTR progress / dip-buying / holdings / overview / "how is it now" → you MUST first run `show_progress_mstr.py`, and paste its output code-block board verbatim, the entire thing unchanged (all four rounds T1/T2/Swan-A/Swan-B, every sub-batch of each round, none omitted), then add a summary. Never condense it into a paragraph, never list only T1, never drop the board.**
2. **The MSTR price and mNAV may only come from that one line in the board; never make them up** (you have no independent market data source; mNAV is computed live by the script). If the board shows "fetch failed", say so honestly and tell the user to open the dashboard — **do not fill in any price/mNAV.**
3. **Bookkeeping may only go through `record_fill_mstr.py` (--round/--price/--shares/--undo). It is absolutely forbidden to use cat/sed/echo/an editor** to directly modify `mstr-state.json`, to invent fields, or to rebuild the ledger. If the script errors, relay it; do not work around it.
4. All holdings/cumulative/average-cost/round-status are authoritative from the script output only.

## Round cheat sheet (two layers; see the round reference table below for details)

**Core pool $14K**: `T1 Opening Salvo (mNAV 0.78–1.0x / $100–145)` · `T2 Green-Line Core ⭐ (mNAV 0.65–0.78x / $80–105)`
**Swan $14K** (after the core pool is full): `A High probability (green line breached, mNAV 0.55–0.7x)` · `B Low probability (mNAV<0.5x)`

## Workflow

### 1. Identify intent

| User says | Intent | Extract |
|-----------|--------|---------|
| "T2 filled at 91, bought 30 shares" / "T1 filled at 130" / "Swan-A filled at 90, got 39 shares" | dip fill | round + price (+ shares) |
| "my mistake" / "undo" / "it didn't fill" | undo | — |
| "MSTR progress" / "investment progress" / "holdings" / "how is it now" / "what's the mNAV" | check progress | — |

### 2. Call the scripts

```bash
S=$HOME/.cockpit/skills/mstr-tracker/scripts
export MSTR_STATE=$HOME/.cockpit/mstr-tracker/mstr-state.json
# The dashboard link is freshly signed by the script (dash_link carries type=mstr + a 24h time-limited code);
# the last line of the echoed board already includes the link — forward it verbatim.
# ⚠️ Do NOT export DASH_URL (setting it would override the freshly signed link).

# Dip fill (most common: round + price + shares)
python $S/record_fill_mstr.py --round T2 --price 91 --shares 30
# Without shares, estimate from that round's planned batch
python $S/record_fill_mstr.py --round T1 --price 130
# Undo the last entry
python $S/record_fill_mstr.py --undo
# Check progress (with live MSTR price + mNAV + each round's batches)
python $S/show_progress_mstr.py

# Monday DCA / user says they want to DCA: first get this week's window-paced suggested amount + the next open batch (read-only, no ledger write)
python $S/dca_advice_mstr.py            # live mNAV
python $S/dca_advice_mstr.py --mnav 0.81   # offline / specified mNAV
```

## Monday / user says they want to DCA → first compute the suggestion, then record it into the core pool as a standalone add-on (window-paced model)

The MSTR **core pool has no separate DCA track**: window DCA = buying the "suggested amount" each week, **recorded as a core-pool DCA standalone add-on** (mirroring the BTC DCA), $14,000 core pool. The Swan rounds A/B **do not participate in DCA**.

⚠️ **A DCA never claims or fills any limit dip leg** (T1-c / T2-a/b/c are deep-discount mNAV limit legs, kept as an overlay; whether to act on a touched price is decided separately). A market DCA goes through `--standalone` and does not touch these limit legs. The flow is **two steps, both required**:

1. **First run `dca_advice_mstr.py` to get this week's suggestion** (read-only, no ledger write): read the last-line JSON's
   - `week_amount_usd` = this week's window-paced suggested amount (remaining core pool ÷ weeks left in window × mNAV tilt; forced fill-up at window end);
   - `record_mode` = `"standalone"` (always standalone: a core-pool DCA add-on, does not claim a dip leg);
   - `overlay_leg` = the currently still-open limit leg (shown only as a deep-discount overlay reference, **not a recording instruction**);
   - `action:"REVIEW"` (the golden window has passed) → relay honestly: "the window has ended; the remaining pool awaits your review", **do not auto fill-up**.
   - `ok:false` (mNAV fetch failed / abnormal) → relay honestly: "mNAV fetch failed, check the dashboard", **never make up the suggested amount/mNAV** (hard rule #2).
2. **After the user buys, use `record_fill_mstr.py --standalone` to record it as a core-pool DCA add-on** (bookkeeping is still the sole write entry, hard rule #3):
   ```bash
   python $S/record_fill_mstr.py --standalone --price {fill_price} --shares {fill_shares}
   ```
   (`--standalone` = a core-pool add-on not attached to any round; it lands in the top-level standalone_fills and is counted into holdings/invested/avg, **without touching any dip leg**. `week_amount_usd` is the "how many dollars to spend this week" budget — the user places their order accordingly, actual shares ≈ `week_amount_usd ÷ fill_price`; the script computes the booked amount as price×shares, no separate amount is passed.)

**few-shot** — User: `How much MSTR DCA this week?` / `It's Monday, time to buy MSTR`
First run `dca_advice_mstr.py`, reply = one suggestion line + standalone bookkeeping guidance:
> 📅 This week's MSTR core-pool DCA suggestion **≈$736** (window pacing · mNAV 0.81x "baseline" tier ×1.0).
> 👉 Tell me once you've bought (e.g. "MSTR DCA at 105, bought 7 shares"), and I'll record it as a core-pool DCA add-on (standalone, does not touch the limit legs).

After the user reports the fill → follow step 2 above, `record_fill_mstr.py --standalone --price {price} --shares {shares}`, then paste the echoed board verbatim (same as the "dip fill" flow).

### 3. Compose the reply from the script output

**🔴 Unified progress management (most important)**: when `record_fill_mstr.py`'s bookkeeping command (fill / undo) **succeeds, stdout = the full updated board code block + a last line of JSON**. Your reply =
1. one confirmation line (use the `msg` from that last JSON line);
2. immediately followed by **the full board code block echoed by the script, pasted verbatim, unchanged** (all rounds T1/T2/Swan-A/Swan-B and all sub-batches + the 📊 link) — that is the updated progress.

**After recording, the user should immediately see the full updated progress without sending "MSTR progress" again.** It's the same board as the progress check — unified.

`ok:false` (no board, just one JSON line) → relay the `msg` in a friendly way (the round couldn't be recognized → ask back; a duplicate or over-budget needs --force; the ledger is abnormal).

## Reply format (few-shot)

**Dip fill** — User: `T2 filled at 91, bought 30 shares`
Call `record_fill_mstr.py --round T2 --price 91 --shares 30`. Reply = one confirmation line + the echoed full board verbatim:
> ✅ Recorded **T2 Green-Line Core** T2-b filled $91 × 30 sh = $2,730. Updated progress:
>
> 〔paste here, verbatim and unchanged, the full ``` board code block echoed by record_fill_mstr (all of T1/T2/Swan-A/Swan-B) + the 📊 link〕

**Check progress** — User: `MSTR progress` / `investment progress` / `holdings` / `how is it now`
Call `show_progress_mstr.py`. The reply **must have two parts; missing part 1 is a wrong reply**:

**Part 1 (required, placed first)**: paste the script stdout **verbatim, the entire thing unchanged** — it is an already-formatted code-block board, **containing all four rounds T1 / T2 / Swan-A / Swan-B, every sub-batch of each round, none omitted. Never condense, never rewrite into a paragraph, never pick only a few T1 batches**. Just paste a full code block like this:

```
MSTR Buy-the-Dip Progress
━━━━━━━━━━━━━━━━━━━━
Holdings 0 sh · Avg —
Invested $0 / $28,000 (0%)
Price $126.55 · mNAV 0.89x · T1 zone
━━━━━━━━━━━━━━━━━━━━
[Core $14K]
○ T1 Opening Salvo 0/3 $0/$6.3K
   ○ T1-a $130 ~14.5sh
   ○ T1-b $118 ~21.4sh
   ○ T1-c $105 ~18sh
○ T2 Green-Line Core⭐ 0/3 $0/$7.7K
   ○ T2-a ≈$111 ~27sh
   ○ T2-b ≈$100 ~30sh
   ○ T2-c ≈$92 ~27sh
[Swan $14K]
· A High probability 0/2 $0/$7K
   · A-1 ≈$100 ~39sh
   · A-2 ≈$85 ~45sh
· B Low probability 0/2 $0/$7K
   · B-1 ≈$71 ~58sh
   · B-2 ≈$57 ~78sh
━━━━━━━━━━━━━━━━━━━━
🎯 Next T1 Opening Salvo (Core)
Untouched $28,000 (100%)
```
📊 [Open live dashboard] (forward the freshly signed link from the last line of the echoed board; do not paste a literal placeholder)

(The ≈ on an order price means it is a floating price derived from the live NAV-per-share against the mNAV threshold, same basis as the dashboard; the numbers differing each time is normal — paste as-is, do not change or explain it.)

**Part 2 (below the board)**: 3–5 summary bullets (**copy the price/mNAV only from that one board line, never invent**):
> Summary:
> - Price $126.55 · mNAV 0.89x (in the T1 zone) — from the board
> - 0 shares held, core pool 0% untouched
> - mNAV has entered the T1 range (0.78–1.0x); can place T1 in tranches ($130/$118/$105)
> - The main force is in the T2 green-line zone (mNAV≤0.78x); wait for a deeper discount to add aggressively

**Undo** — User: `my mistake`
Call `--undo`, reply "↩️ Undid the last entry" + the echoed full board verbatim (post-undo progress).

**Ambiguity / anomaly**:
- Only a price was given, no round, and the price range overlaps (e.g. $90 sits between T2/Swan-A) → **ask back: "Is this T2 or Swan-A?"**
- Script `ok:false` with a prompt "add --force to confirm" (duplicate/over-budget) → relay, and wait for the user's confirmation before adding --force.
- Ledger corrupted → say so honestly and let the user handle it; **do not fix it yourself.**

## Safety red lines

- **Read-only monitoring; never place / cancel / modify orders** — all broker actions are done manually by the user.
- Bookkeeping only goes through `record_fill_mstr.py`; if unsure about the round/price/shares, **always ask back**, never guess — this is real money.
