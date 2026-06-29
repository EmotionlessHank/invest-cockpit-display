# Dip-buy tier table (claim reference)

> This table maps a verbally reported fill price to its tier ("report fill price -> claim tier"). The tier short-names below (`B1 probe`, `B2 core`, ...) are the literal labels the scripts emit and store in `state.json`; the scripts and dashboard read them as data keys.

## Claim rule

Reported fill price -> falls into a range below -> that is the tier. When no amount is given, match the leg amount of the split-order plan whose price is closest to the fill price.

| Tier | Trigger range | Total | Split-order plan |
|----|-----------|-----:|-------------|
| **B1 probe** | $66,000–70,000 | ¥12,000 | $69K ¥6K (filled) · $66K ¥6K |
| **B2 core** | $60,000–66,000 | ¥28,000 | $64K ¥14K · $61K ¥14K |
| **B3 green line** ⭐ | $55,000–60,000 | ¥48,000 | $59K ¥16K · $57K ¥16K · $55K ¥16K |
| **C1 deep dip** | $52,000–55,000 | ¥16,000 | $54K ¥8K · $52K ¥8K |
| **C2 deep dip** | $48,000–52,000 | ¥14,135 | $51K ¥7K · $48K ¥7K |

> B3 green line carries the largest weight (¥48K), centered on the on-chain "green line" $59–60K "final flush".
> Below $48,000: BTC is treated as having bottomed at the 200k floor; no further adds.

## DCA five tiers (track A)

| BTC spot | Tier | Amount/week |
|---------|------|--------:|
| > $80K | Pause | — |
| $70–80K | Cautious | ¥3,000 |
| $58–70K | Heavier | ¥5,500 |
| $50–58K | Double | ¥7,500 |
| < $50K | Enhanced | ¥10,000 |

> The DCA tier labels (`Pause/Cautious/Heavier/Double/Enhanced`) are produced by the strategy module's `TIER_LABEL` and are the verbatim values the system emits.

## Linkage rules (auto-applied after a verbal fill)

- **Rule ②**: a dip filled in the last 7 days -> this week's DCA auto-downgrades one tier (Double->Heavier->Cautious), freeing the savings for deeper dip tiers.
- **Rule ③**: cumulative invested ≥ ¥180,000 (90%) -> DCA pauses, preserving dip dry powder.
- Hard cap ¥200,000, never to be breached.

> record_fill.py reports the linkage after recording; the concrete DCA amount is computed by the live cockpit from the current spot price.
