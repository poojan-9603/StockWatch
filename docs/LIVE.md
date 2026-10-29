# Live scoring and paper verdicts

The frozen design (docs/M3.md) running on live data in paper mode, as hard rule 1 allows since 2026-10-27: the latest day only, inside `quant.lock.live`.

## What runs

Inside the nightly run, after the SEC refresh, prices and checks all pass (bad data, no verdicts). Code: `quant/live.py`.

- **Every night:** scores for every stock in the live universe (2,309 stocks in October 2026) go to `market/scores/latest.parquet`, for the site and the agent panel. Not a record.
- **On a month's last trading day,** if the data is in before the next open:
  - that day's scores are saved once to `market/scores/month_end/<day>.parquet`
  - the frozen simulator replays every saved month-end, so the paper portfolio follows from the snapshots and prices alone
  - that day's decisions are appended to the hash-chained verdict log: Buy (with size), Sell (with the reason), Trim and Hold, marked book "paper"
- **Fills are at the next open,** as in the backtest. The paper portfolio is the scoreboard's forward hold-out.
- **The first paper month-end is Friday, Oct 30, 2026.** Its decisions fill at the open on Monday, Nov 2.

## Decisions I made (yours to veto)

1. **Universe:** the plan's live universe (NYSE or Nasdaq US common stock, over $5 and $300M, enough volume), ranked across all of it. S&P 500 members and everything else are labeled separately for the scoreboard, as the plan says. The backtest ranked within the S&P 500 only, so live ranks aren't the backtest's ranks.
2. **Monthly decisions,** because FROZEN rebalances monthly. Between month-ends, scores still update every night.
3. **A month-end whose data arrives after the next open gets no decisions that month,** said loudly. Deciding late but recording a fill at the earlier open would be a quiet look-ahead.
4. **The replay must match the log.** If revised data would change an earlier month's recorded Buys or Sells, nothing new is written and the run fails loudly.
5. **News check:** recorded as "not checked (no news source yet)". The frozen design had none, and there's no news source yet.
6. **Trading calendar:** NYSE's published holiday rules in code. An unscheduled closure (a national day of mourning) isn't known in advance; if one lands on a month-end, the decision falls on the wrong day.

## Checks so far

- **Tests:** a 40-stock synthetic market run end to end, plus the calendar, the live lock, reruns, late data and a replay that doesn't match.
- **Simulator unchanged:** after adding the decision trace, the 2018 to 2019 rehearsal ([run 40801392831](https://github.com/poojan-9603/StockWatch/actions/runs/40801392831)) matched the Oct 27 rehearsal to the last digit.
- **Real data:** scored 2,174 of 2,309 stocks for 2026-10-28 in a manual nightly ([run 40803833679](https://github.com/poojan-9603/StockWatch/actions/runs/40803833679)). The rest lack 5 of the 7 signals.

## Not yet

- **Real holdings get no verdicts yet,** only the paper book. That comes once trades exist.
- **The site, the panel's weekly job and the daily summary** read these scores and verdicts once they're built.
- **S&P 500 membership** comes from the M1 backfill and isn't refreshed yet. It only sets the slice label, not the decisions.
