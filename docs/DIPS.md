# Leader dips: are big leaders that fall 20 to 25% buyable again?

Your question (PLAN.md, changes 2026-11-05), measured before building any alert. Code `quant/dips.py`, run [42155619341](https://github.com/poojan-9603/StockWatch/actions/runs/42155619341) on 2013 to 2019 only (test years locked). Logged as an experiment.

**How:** every Friday, each S&P 500 member's drop from its 1-year high. A dip is the first Friday it's 15, 20, 25 or 30% below, re-armed once it's back within half that. Then its return over the next 3, 6 and 12 months against SPY, and how often it fell another 10% first. 4,630 dips.

- **Leaders:** the 100 biggest S&P 500 companies that month.
- **Strong industry:** the top third of industries by 12-month return.
- **Tech:** computers, semiconductors and software. That's the only part of "AI and semiconductors" that industry codes can name.

## Results (return against SPY)

| Group, 20% dip | Dips | Companies | 3 months | 6 months | 12 months | Beat SPY at 3m | Fell 10% more within 3m |
|---|---|---|---|---|---|---|---|
| Every S&P 500 stock | 1,221 | 500 | -1.6% | -2.5% | -3.5% | 46% | 38% |
| Leaders, any industry | 200 | 101 | +0.2% | -1.8% | -1.8% | 51% | 36% |
| Leaders, strong industry | 59 | 43 | +0.2% | +0.3% | -1.3% | 54% | 31% |
| Leaders, tech | 36 | 17 | +4.8% | +7.0% | +7.8% | 69% | 25% |
| *Tech leaders on any Friday, no dip* | 4,153 | 21 | +1.3% | +2.5% | +5.6% | 57% | 23% |

At 15%, 25% and 30% the picture is the same. The 25% and 30% groups get small (9 to 19 dips for leaders in strong industries), so they can't tell anything apart.

## What it means

1. **A dip on its own is not a buy signal.** Across all S&P 500 stocks, a 20% drop was followed by doing *worse* than the market (t about -1.5 to -2.2). Stocks that fall tend to keep lagging.
2. **For big leaders, a dip is about neutral.** They did about as well after a dip as on any other day (within luck). Being big protected them, but the dip added nothing.
3. **A "strong industry" adds nothing measurable.**
4. **Tech leaders did best, but mostly because 2013 to 2019 was tech's decade.** They beat SPY on ordinary days too (+5.6% a year). After a dip, the extra on top of that is a few points, from 17 companies, within luck (t 1.5 to 2.0 against SPY, less against the no-dip baseline). Picking "tech" also uses hindsight: we know now that tech boomed then.
5. **Even the best group often kept falling.** 1 in 4 tech-leader dips fell another 10% within 3 months, and 4 in 10 within a year.
6. **Not covered:** 2020 onward (locked), so neither the 2020 crash nor 2022's tech bear market is in here. 2013 to 2019 was mostly a rising market, which flatters buying dips.

## Decision (2026-11-06)

You chose to build it as a heads-up. The numbers don't support telling you a leader is "buyable again" because it fell 20 to 25%, so the watch says what history showed instead.

**How the watch works** (every night, latest day only, inside the live lock):

- `quant.live` writes today's 100 biggest S&P 500 companies, defined as in the study, with their drawdown from the 1-year high, to `market/scores/leaders.parquet`.
- `pipeline/site.py` keeps the ones 20% or more below their high that are in a strong industry or one of your themes. Themes are in `config/settings.json` (`dip_themes`), as SIC code prefixes or tickers. They start as chips and electronics (367), computers (357) and software and internet (737), the study's tech group.
- Each one shows on Picks under "Big leaders down from their high", with the study's 20% numbers for its group: tech leaders, leaders in strong industries, or the biggest companies overall. A stock newly over the line since the day before is marked "New" and named on Today.

**Limits:** the odds are from 2013 to 2019 only. "AI" has no industry code, so it means the three tech codes plus any tickers you add. A stock whose day has quarantined data is left out.
