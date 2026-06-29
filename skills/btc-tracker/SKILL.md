---
name: btc-tracker
description: "Hank's BTC dip-buying progress tracker and ledger. [When to use] When the user asks about investment progress / progress / dip-buying progress / overview / holdings / 'how are things now' / 'how far did the dips go' / 'how much dry powder is left' / 'open the cockpit' -> run the script to produce the progress board (with live spot price) + cockpit link. When the user verbally reports a limit-order fill (e.g. '69000 filled', 'bought at 64000'), reports that a DCA order was placed, or asks to undo -> record it, claim the tier, and apply the DCA linkage. **Anything touching Hank's BTC dips / DCA / holdings / investment progress MUST go through this skill's scripts. Never answer from memory or make up numbers.** Read-only monitoring, zero order placement."
version: 1.1.0
author: Hank
triggers: ["/btc", "/dip", "/progress", "/overview", "/holdings", "/cockpit", "investment progress", "progress", "dip progress", "holdings", "overview"]
platforms: [linux]
metadata:
  agent:
    tags: [btc, crypto, tracker, dca]
---

# BTC Dip Tracker — conversational ledger + live cockpit

Hank places GTC dip orders **manually** on Binance and does manual DCA. This skill lets him report in plain language; you record the entry, compute the linkage, and hand back a cockpit link. **Read-only monitoring — never place an order.**

> Note: the trigger keywords above are functional matchers (slash commands + natural-language phrases the user types in the chat client), not prose.

## 🔴 Iron rules (break one and the answer goes off the rails)

1. **Whenever Hank asks about BTC investment progress / dips / holdings / overview / "how are things now" -> you MUST first run `show_progress.py` and answer from the script output. Never improvise from memory.**
2. **The spot price may only come from the board line that reads "spot $… · in zone X · ±%". Never invent a BTC price** (you have no independent market feed; a made-up price will be wrong, e.g. a bogus $113K). If the board shows "spot: see cockpit (price fetch failed)", say truthfully that the price fetch failed and ask the user to open the cockpit — **do not fill in any price.**
3. All holdings / cumulative totals / average cost / tier status come strictly from the script output.
4. **Recording is only allowed via `record_fill.py` (`--price` / `--dca` / `--undo`). Never use `cat`/`sed`/`echo`/an editor** to edit `state.json` directly, never hand-assemble the fills field, never rebuild the ledger — manual edits corrupt the base position and cumulative totals and desync from the cockpit. There is exactly one path to change data: call the script. If the script errors, relay its `msg` to the user; do not work around it by hand.
5. **The DCA amount is dynamic (5 tiers by BTC spot + a one-step downgrade if a dip filled in the last 7 days); it is not a fixed 3,000.** Before recording DCA or sending a reminder you **must first call `dca_advice.py` to get this week's suggested amount** (read its `week_amount_rmb`); never default to 3,000 from memory. If the user states an amount themselves, **the user's amount wins**.

## Tier quick reference (see references/dca-tier-table.md)

`B1 probe $66–70K` · `B2 core $60–66K` · `B3 green line $55–60K (key)` · `C1 deep dip $52–55K` · `C2 deep dip $48–52K`

> Tier short-names (`B1 probe`, `B2 core`, `B3 green line`, `C1/C2 deep dip`) are the literal labels the scripts emit and store in `state.json`; the scripts and dashboard read them as data keys, so they must match the ledger verbatim.

## Workflow

### 1. Identify intent (four kinds)

| What the user says | Intent | Extract |
|---------|------|------|
| "69000 filled", "B2 bought at 64000", "the 64000 order filled" | dip fill | fill price (amount if needed) |
| "DCA done", "placed this week's DCA", "this week's DCA is complete", "DCA'd 8000" | DCA placed | amount (if unstated -> first call `dca_advice.py` for this week's suggestion) |
| "I misspoke", "undo that last one", "it didn't fill" | undo | — |
| "investment progress", "progress", "dip progress", "overview", "holdings", "how are things now", "how far did the dips go", "how much dry powder is left", "show/open the cockpit" | check progress | — |

> If you're unsure of the intent but the topic is BTC investing / dips / holdings -> **default to "check progress"** and run the script; don't improvise.

### 2. Call the scripts (run in the terminal, read the JSON on stdout)

> Every script takes the `BTC_STATE` env var prefix pointing at the ledger file **shared** with the cockpit. The cockpit link is **freshly signed by the script** (`dash_link`, auto-includes a static token + a 24h time-limited code) — no need to fill it in, and don't assemble it yourself.

```bash
S=$HOME/.cockpit/skills/btc-tracker/scripts
export BTC_STATE=$HOME/.cockpit/btc-tracker/state.json
# The cockpit link is signed live by the script (dash_link auto-adds token + 24h code);
# the progress/record output's last board line already contains the link — forward it verbatim.
# ⚠️ Do NOT export DASH_URL (setting it overrides the live signature and may send a stale/fixed link).

# Dip fill (without an amount it auto-matches the planned leg by price)
python $S/record_fill.py --price 64000
# Pass the amount when the user states it explicitly
python $S/record_fill.py --price 64000 --rmb 14000
# User reports a leg by sub-label (e.g. "B2-a filled") -> use --label for an exact claim; if no price was given, use that leg's trigger price (it's on the board)
python $S/record_fill.py --price 64000 --label B2-a
# User's amount is in U / USDT (e.g. "spent 2000U") -> pass --usdt verbatim; the script converts to RMB by the ledger's rate;
# ⚠️ never multiply by an FX rate yourself to make --rmb (the conversion is unified inside the script)
python $S/record_fill.py --price 64000 --usdt 2000
# This week's suggested DCA amount (dynamic tier; fetch before recording DCA / sending a reminder, read week_amount_rmb, don't fill 3000 from memory)
python $S/dca_advice.py
# DCA placed (amount = week_amount_rmb from dca_advice above; if the user reported a different amount, use theirs)
python $S/record_fill.py --dca --rmb 5500
# Undo the last entry
python $S/record_fill.py --undo
# Check progress (prints a text board + cockpit link)
python $S/show_progress.py
```

### 3. Compose the reply from the script output

**🔴 Unified progress management (most important)**: when `record_fill.py`'s recording commands (fill / DCA / undo) **succeed, stdout = the full updated board code block + a final JSON line**. Your reply =
1. one confirmation sentence (use the `msg` from that last JSON line, e.g. "✅ Recorded B2 core fill $64,000 / ¥14,000");
2. immediately followed by **the complete board code block the script handed back, pasted verbatim with no edits** (all tiers leg-by-leg + the 📊 link) — that is the updated progress.

**After recording, the user should immediately see the full updated progress — no need to ask for "progress" again.** This is the same board you'd see when checking progress; they are unified.

`ok:false` (no board, just one JSON line) -> relay the `msg` in a friendly way (price out of range / amount needed / duplicate or over-budget needs `--force` / ledger anomaly).

## Reply format (few-shot)

**Dip fill** — user: `64000 filled`
Call `record_fill.py --price 64000`. Reply = one confirmation sentence + the full board pasted verbatim:
> ✅ Recorded **B2 core** fill $64,000 / ¥14,000. Updated progress:
>
> [paste the complete ``` board code block + 📊 link returned by record_fill verbatim, no edits]

**Fill by sub-label (priced in U)** — user: `B2-a filled, spent 2000U`
Call `record_fill.py --price 64000 --label B2-a --usdt 2000` (take B2-a's trigger price from the board; the amount is in U so pass `--usdt` verbatim, **never multiply by ~7 yourself**). Reply = one confirmation sentence + the full board verbatim.

**DCA placed / this week's DCA complete** — user: `this week's DCA is done` (no amount given)
First call `dca_advice.py` to get this week's suggested amount (e.g. spot $63K lands in the "Heavier" tier, but a dip filled in the last 7 days -> rule ② downgrades to "Cautious" ¥3,000), then call `record_fill.py --dca --rmb 3000`, and reply "✅ Recorded DCA ¥3,000 (Heavier tier, downgraded because a dip filled in the last 7 days)" + the full board verbatim.
　If the user reports an amount themselves (e.g. `DCA'd 8000`) -> go straight to `--dca --rmb 8000`, **their amount wins**, no need to ask again.

**Undo** — user: `I misspoke just now`
Call `--undo`, reply "↩️ Undid the last entry" + the full board verbatim (the post-undo progress).

**Check progress** — user: `progress`
Call `show_progress.py` and **forward verbatim** the code-block board + cockpit link it outputs (already formatted, no wrapping on mobile, with the live spot price + each tier's sub-plan leg-by-leg ✅filled/○pending/·deep-dip-waiting), then add a "summary" (3–5 plain-language judgments with a conclusion; **the spot price may only be quoted from that one board line — never invent another**):
> [forward the code-block board + 📊 cockpit link verbatim]
>
> Summary:
> - Spot $66,883 (in the B1 zone, down 2.4%) — number taken from the board, do not change it
> - Budget 26% used, ¥51,865 cumulative, ¥148,135 dry powder untouched (74%)
> - Dip orders all 0/N resting and waiting to trigger; the most recent is at $69K (B1 ¥6K)
> - Spot has entered the B1 zone; the $69K leg could fill any time — watch it
> - The heavy B3 green line⭐ (¥48K) sits at $55–60K and still needs to fall further; plenty of dry powder, no rush

> Full templates for all four reply kinds are in `references/tg-message-templates.md`.

**Ambiguity / anomalies**:
- User just says "it filled" without a price -> ask back: "Which price-level order filled?"
- Price not in any tier range -> relay the msg, point to the valid tiers.
- Script returns `ok:false` with a msg saying "add --force to confirm" (duplicate report / over the budget cap) -> **first relay the msg and wait for explicit confirmation**, then re-run **the same command with `--force`** once confirmed; don't add `--force` until the user confirms.
- Script reports "ledger corrupted / missing field" -> tell the user truthfully that the ledger is anomalous, **do not try to fix it yourself**, let Hank handle it (the ledger can only be repaired outside the agent platform).

## Safety red lines

- **Read-only monitoring — never place / cancel / modify an order.** All Binance actions are done by Hank manually.
- Touch no trading keys; the scripts only read/write `state.json`.
- When unsure of a fill price / amount, **ask back to confirm** — never guess. This is a real-money ledger.
