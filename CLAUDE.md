# invest-cockpit — engineering conventions

Conventions for working in this repo (human or AI contributors).

## Red lines

1. **The ledger is authoritative server-side.** Only `seeds/*.seed.json` (fictional demo data) live in the repo. No deploy or sync ever pushes over or overwrites the production `state.json`.
2. **One write path.** All ledger mutations go through the single write-entry script; the web layer (`app.py`) never edits the ledger directly — it shells out to that script.
3. **Secrets never enter git, logs, or chat.** `DASHBOARD_SECRET`, tokens and real ledger data stay out of the tree (see `.gitignore`).

## Development

- **Single test entrypoint:** `./run-tests.sh` — also the pre-deploy gate (a red suite blocks deploy). Fix-and-reproduce first when touching write scripts (TDD).
- **Single source of truth for shared modules:** `strategy.py` / `mnav.py` live in `app/` and are distributed to skill scripts at deploy time. Do not keep duplicate copies under `skills/`.
- **`{DASH_URL}` in SKILL.md is a placeholder** injected at deploy time; keep it as-is in the repo.
- The conversational layer runs on an LLM (DeepSeek in production); keep skill prompts explicit, example-driven, and strict about output format.
</content>
