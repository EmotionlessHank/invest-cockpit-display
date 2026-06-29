# TG message templates (for composing replies)

> The server-side AI runs on DeepSeek, which does not infer intent on its own, so **the template is the result**. This file is the standard structure + few-shots for the `btc-tracker` skill's four reply kinds, mirrored into `SKILL.md`.
> Principle: forward the script's code block **verbatim** (already formatted, no wrapping on mobile); DeepSeek only adds **plain-language judgments** below it — give a conclusion, don't pile up numbers; English; read-only monitoring, no order placement.
>
> Note: tier short-names (`B2 core`, `B1`, ...) and DCA tier labels (`Heavier/Cautious/...`) below are the literal values the scripts emit and store; see `dca-tier-table.md` for the full table.

---

## 1. Check progress ("progress / overview / how are things now")

Call `show_progress.py`, **forward verbatim** the code-block board + cockpit link it outputs, then add a "summary" (3–5 points).

**The board already contains**: live spot price (spot $… · in zone X · ±%), the total-budget progress bar, holdings/average cost, each tier's sub-plan leg-by-leg (✅filled / ○pending / ·deep-dip-waiting), the next trigger price, and untouched dry powder.
**🔴 Spot-price iron rule**: in the summary, the spot price may **only be copied from that one board line** — never invent another (you have no independent market feed). If the board shows "spot: see cockpit (price fetch failed)", say truthfully that the price fetch failed and ask the user to open the cockpit; do not fill in any price.

```
{full output of show_progress.py}

Summary:
- Spot ${board price} (in zone X, ±X%) — copied from the board
- Budget: invested ¥X (X%), dry powder untouched ¥Y (Z%)
- Dip orders: {which tier/leg filled / which leg is still missing / all resting and waiting}
- Next: ${trigger price} -> {tier} ¥{amount}
- One line: {judgment on spot vs tier / dry-powder status / whether to top up USDT}
```

**few-shot** (post-reset state + live price $66,883):
> [forward the code-block board + 📊 cockpit link verbatim here]
>
> Summary:
> - Spot $66,883 (in the B1 zone, down 2.4%) — taken from the board
> - Budget 26% used, ¥51,865 cumulative, ¥148,135 dry powder untouched (74%)
> - Dip orders all resting and waiting; the most recent is $69K (B1 ¥6K)
> - Spot has entered the B1 zone; the $69K leg could fill any time — watch it
> - The heavy B3 green line⭐ (¥48K) at $55–60K still needs to fall further; plenty of dry powder, no rush

---

## 2. Dip fill ("64000 filled", "B2 bought at 64000")

Call `record_fill.py --price 64000` (without an amount it auto-matches the planned leg by price); follow the returned JSON:

```
✅ Recorded {tier} fill ${price} / ¥{amount} (+{btc} BTC)
Cumulative invested ¥{X} · holdings {X} BTC · avg cost ${X} ({X}% used)
{if partial: this tier still needs {remaining legs}; if done: ✅ {tier} fully filled}
📉 Linkage: a dip filled recently -> this week's DCA auto-downgrades one tier (savings go to deeper dip tiers)
🎯 Next tier: {tier} ${range}
📊 Cockpit: <the live-signed link from the board's last line, forward verbatim, don't fill literally>
```

**few-shot** — user: `64000 filled`
> ✅ Recorded **B2 core** fill $64,000 / ¥14,000 (+0.0313 BTC)
> Cumulative invested ¥71,865 · holdings 0.1518 BTC · avg cost $67,654 (35.9% used)
> This tier still needs one $61K leg, ¥14K
> 📉 Linkage: a dip filled recently -> this week's DCA auto-downgrades one tier
> 🎯 Next tier: B2 core has $61K left, then deeper B3 green line⭐ $55–60K (¥48K heavy)
> 📊 Cockpit: <the live-signed link from the board's last line, forward verbatim, don't fill literally>

---

## 3. DCA / this week's DCA complete ("DCA'd", "this week's DCA is done")

The amount is **dynamic** (5 tiers by spot + a downgrade if a dip filled in the last 7 days), **not a fixed 3,000**: if the user reports no amount -> first call `dca_advice.py` to get `week_amount_rmb`, then `record_fill.py --dca --rmb {suggested}`; if the user reports an amount -> use theirs.
> ✅ Recorded DCA ¥{amount} ({tier} tier{if a dip filled in the last 7 days: · rule ② already downgraded}, +{btc} BTC). Cumulative invested ¥{X} · holdings {X} BTC.

---

## 4. Undo ("I misspoke / undo that last one / it didn't fill")

Call `record_fill.py --undo`:
> ↩️ Undid the last entry; cumulative back to ¥{X} · holdings {X} BTC.

---

## 5. Monday DCA reminder (cron-triggered, every Monday 11:00 Sydney, no user prompt needed)

Cron calls `dca_advice.py` -> injects the output into this prompt -> wraps it as a reminder and pushes it to TG. **Copy the amount from the script's `week_amount_rmb`; never compute the tier or invent the spot yourself**; if the script returns `ok:false` (price fetch failed) -> say truthfully that the fetch failed and ask the user to open the cockpit, fill in no price/amount.
> 📅 *This week's DCA reminder*
> Suggested DCA **¥{week_amount_rmb}** ({tier} tier{if downgraded: · a dip in the last 7 days already downgraded one tier})
> Spot ${btc_price} -> lands in the "{base_tier}" base tier
> After placing the order, reply "this week's DCA is done" and I'll record it.
> 📊 Cockpit: <the live-signed link from the board's last line, forward verbatim, don't fill literally>

**few-shot** (dca_advice output week_amount_rmb=3000 / tier=Cautious / base_tier=Heavier / downgraded=true / btc_price=63750):
> 📅 *This week's DCA reminder*
> Suggested DCA **¥3,000** (Cautious tier · a dip in the last 7 days already downgraded one tier)
> Spot $63,750 -> lands in the "Heavier" base tier (would be ¥5,500, lowered to Cautious because of the recent dip)
> After placing the order, reply "this week's DCA is done" and I'll record it.
> 📊 Cockpit: <the live-signed link from the board's last line, forward verbatim, don't fill literally>

---

## Ambiguity / anomalies

- Just "it filled" without a price -> ask back: "Which price-level order filled?"
- Price not in any tier range -> relay the script's `msg`, point to the valid tiers.
- This is a real-money ledger; when unsure of a price/amount **always ask back** — never guess.
