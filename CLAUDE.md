# CLAUDE.md

Personal US stock research app. Advice only, never places trades. Full design in `docs/PLAN.md` and `docs/ARCHITECTURE.md`. Read both before starting any milestone.

## How we work

- **One milestone at a time** (M0 to M8 in PLAN.md). Stop at each milestone's "done when" check and summarize: what passed, what failed, what needs my decision. Don't start the next milestone until I say so.
- **The plan is the spec.** If something in it is wrong, impossible, or more complex than it looks, stop and tell me. Never work around it silently or swap in a different approach on your own.
- **Measure, don't assume.** When choosing between options (libraries, thresholds, data sources), test them on real data and show me the numbers.
- **Simplest thing that meets the requirement.** No extra abstractions, services, or dependencies the plan doesn't call for. But never skip a requirement.
- **Explain the why** in summaries and PR descriptions, not just the what.

## Hard rules

1. **Test years are locked.** Never run backtests, tuning, or any evaluation on data from 2020 onward. Tuning uses 2013 to 2019 only. The one 2020+ test (M3b) ran on 2026-10-27 and is never rerun. The one exception is live scoring of the latest day for paper mode and the agent panel (approved 2026-10-27), only inside `quant.lock.live`, which refuses any past day.
2. **Point in time.** No calculation may use a number before its filing date, or a company outside the universe as of that date. Keep the point-in-time test passing.
3. **Append-only.** Never UPDATE or DELETE rows in the SQLite records. Corrections are new rows pointing at the old ones.
4. **One writer.** Only the pipeline writes the database. The site and the Worker never do.
5. **No silent failures.** Retry, then log loudly. Bad data blocks verdicts; it never gets guessed around.
6. **Secrets** live only in GitHub secrets and Cloudflare's secret store. Never in code, logs, commits, or chat.
7. **Respect rate limits.** Every source gets a limiter. SEC requests carry a User-Agent with contact info and stay under 10 a second.
8. **Agents have no powers.** LLM calls get documents in and structured answers out. No tools, no network, no writes. Every claim's quote is verified by code.
9. **No real money changes.** Never add trade execution or broker connections.

## Writing

- Never use em dashes in anything written for me (docs, READMEs, commit messages, UI copy). Use commas, periods, or parentheses.
- READMEs and docs: short, candid about findings and limitations, plain English.
- Log every backtest variation in the experiments log, including failures.

## Stack

Python (pipeline, quant, agents), DuckDB + Parquet (market data), SQLite (records), GitHub Actions (schedule), Cloudflare R2, Pages, Worker, Access, React + Vite + TypeScript (site), TradingView Lightweight Charts, Claude API (Haiku 5.5 routine, stronger model as judge, $10 monthly cap).
