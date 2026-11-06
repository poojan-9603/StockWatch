# Plan

A personal stock research app for US stocks. It scores stocks with tested math, gets an independent second opinion from an AI agent panel, watches what I own, and tells me plainly when there's nothing to do.

Advice only. It never places trades. I'm not a financial advisor and neither is this app.

## Principles

- **Honest over busy.** "No action needed" and "no buys this week" are normal answers. No minimum number of picks, ever.
- **Math decides, agents advise.** The verdict comes from tested code. The agent panel's view is shown side by side and earns a vote only through a live track record.
- **Nothing is trusted on the data it was tuned on.** Tune on 2013 to 2019, test once on 2020 to now.
- **No silent failures.** Bad data means no verdicts that day, said plainly.
- **History can't be rewritten.** Trades and verdicts are append-only. Corrections are new entries.
- **Simple wins.** No servers, no laptop dependency, $0 infrastructure.

## Budget

| Item | Cost |
|---|---|
| Data | $0 (free sources) |
| Infrastructure | $0 (GitHub Actions, Cloudflare free tiers) |
| LLM (Claude API) | about $3 to $8 a month, hard cap $10 |
| Domain (only if Cloudflare Access needs one) | about $10 a year |

## Decisions

### Step 1: Data

**What we fetch, and why each exists**

| Data | Feeds | Source |
|---|---|---|
| Daily prices and volume | Momentum, volatility, sizing, correlation | yfinance, Tiingo fallback |
| Fundamentals | Quality, value, earnings surprise, issuance, asset growth | SEC EDGAR bulk files |
| 8-K filings | Red flags, catalysts | SEC EDGAR |
| News headlines | News door, agent checks, briefing | Finnhub free |
| Earnings dates | Timing, briefing | Finnhub (free: 1 month history plus upcoming) |
| Macro (rates, volatility) | Market mood filter | FRED |
| Sector codes | Sector-relative ranks, sector cap | EDGAR |
| Splits and dividends | Correct returns and holdings | Tiingo for history (including dead companies), yfinance for daily |
| My trades | Holdings | Entered by me |
| Dead-company prices | Backtest without survivorship bias | Tiingo free |

Insider buying (Form 4) is a v2 candidate, tested before use. Social sentiment and paid analyst estimates are out.

**Which stocks**

- Live universe: NYSE or Nasdaq, common stock, US companies (no ADRs), price above $5, market value above $300M, minimum daily volume. Rebuilt monthly.
- Backtest universe: S&P 500 members as of each past date, including removed and dead companies. Membership history from a free dataset, cross-checked against a second free source.
- Small caps are recommended live even though the backtest covers large caps only. My choice, made knowing that slice is untested. Tracked separately on the scoreboard.

**History:** 2010 onward. Full XBRL fundamentals coverage for smaller companies starts mid-2011, so tuning runs 2013 to 2019 and the test runs 2020 to now. The 2008 crisis is not covered.

**Pipeline rules**

1. Backfill and daily update are separate jobs.
2. Use bulk files where offered (EDGAR companyfacts.zip, submissions.zip).
3. Every source gets a built-in rate limiter. SEC: max 10 requests a second with a User-Agent containing contact info.
4. Backfills checkpoint and resume, never restart.
5. Save raw responses first, clean second.
6. Idempotent writes keyed on (company ID, date).
7. Retry with exponential backoff, then log the failure loudly. Never skip silently.
8. Every row records its source and fetch time.
9. Store raw prices plus a splits and dividends table. Do the adjustment math ourselves.
10. Companies are keyed by CIK. Ticker mappings carry date ranges, since tickers get reused.

**Quality checks**

- Completeness against the market calendar
- Sanity: price above zero, high at or above low, close within range
- Moves over about 40% must be explained by a split, an 8-K, or a second source, or get flagged
- Staleness: repeated closes flagged
- Daily cross-source spot check (yfinance against Tiingo) on 20 to 30 stocks drawn from a fixed pool of about 150, rotated monthly to stay under Tiingo's 500-symbol monthly cap
- Accounting identity: assets equal liabilities plus equity
- A test that fails if any calculation uses a number before its filing date

Failures are sorted into **block** (no verdicts today), **quarantine** (skip that stock), or **log**.

### Step 2: Storage

| Piece | Decision |
|---|---|
| Market data | Parquet files queried with DuckDB |
| Irreplaceable data (trades, holdings, verdicts) | SQLite, append-only enforced by triggers, fingerprint (hash) chain on the verdict log |
| Runs on | GitHub Actions, private repo |
| Stored in | Cloudflare R2 free tier |
| Language | Python for pipeline and math |
| Backups | Daily SQLite snapshots in R2 (30 daily plus monthly), nightly text export of irreplaceable tables to a second private GitHub repo, monthly automated restore test |

Dead-company prices from Tiingo are treated as irreplaceable, since re-fetching them could take months under the 500-symbol limit.

The SQLite-vs-DuckDB split gets benchmarked on real data. If SQLite alone is fast enough, we drop to one tool.

### Step 3: Quant engine

**Signals (7):** momentum (12 months, skipping the latest month), quality, value, low volatility, earnings surprise drift (against the company's own trend), share issuance, asset growth. Each must work on both the tuning and test periods or it's dropped.

**Combining**

- Percentile rank per signal. Fundamentals ranked within sector, price signals across the market.
- At least 5 of 7 signals present to be scored. Missing counts as 50.
- Equal weights. Optimized weights and ML models are challengers that must win clearly on held-out data.

**News**

- News can block or downgrade a buy at any score.
- Strong positive news from a filing or established outlet can tip a borderline stock (about 62 to 79) to a buy. It can never rescue a weak one. Company press releases alone can't tip.
- **Catalyst path:** huge confirmed news (large relative to company size, confirmed by an 8-K or official announcement) triggers a deep read and an alert labeled "news-driven, not score-backed." I decide. Max half normal size. Tracked separately.

**Technical timing layer:** for stocks that already earned a buy, checks for short-term overextension, trend (200-day average), and recent breakdown. Kept only if the backtest supports it.

**Verdicts**

- Not owned: Buy, Wait, Don't buy. Owned: Hold, Add, Trim, Sell.
- Buy requires all of: score 80 or higher, at least 5 of 7 signals individually above 60, news check passed, timing passed, market filter passed, room in the portfolio.
- Keep holding above about 50. Sell consideration below about 40, confirmed across two weekly checks.
- Each buy gets an expected horizon from signal decay (momentum-driven: months; quality and value: 1 to 3 years) and a review date.
- 1-year long-term flag per lot before any trim (a reminder, not tax advice).
- Every verdict shows signal agreement, missing data, and whether news changed it.

**Sizing**

- 10 to 20 positions.
- Volatility-adjusted base size, nudged by score by at most 20%. The nudge stays only if it helps on held-out data.
- Cap: 1.5 times the average position, never above 15%. Minimum 2%. Catalyst picks at half size.
- Sizes are a percentage of the money I tell the app it manages (set in Settings).

**Risk**

- Sector cap about 30%.
- Hidden-twin check: correlation above about 0.8 with an existing holding gets flagged.
- Soft market filter: in a bad mood (S&P 500 below its 200-day average with high volatility), raise the buy bar and shrink new positions. Never force-sell. Compared against always-invested and a hard filter in the backtest.
- Drawdown alarm at 20% below peak triggers a review, never an auto-sell.

**Selling**

- Confirmed score drop, thesis broken (the reason for buying collapsed), serious red-flag news, size over cap (trim), much better candidate waiting (swap only if 20+ points better).
- No "sell after +50%" rule.
- No automatic stop-loss. A big drop triggers a review. A volatility-scaled stop is tested as a challenger.
- Wash-sale and tax-loss flags as reminders.

**Backtest rules**

1. Test years run once, after the design is frozen.
2. Trade at the next day's open. Fundamentals count from filing date. Universe as of each date.
3. Costs: about 0.1% per trade for large caps, more for small caps. Turnover reported.
4. Delistings: acquired companies exit at the final price, failed companies take a heavy penalty (about 30% below the last price).
5. Benchmarks: S&P 500 with dividends, and equal-weighted S&P 500.
6. Every experiment is logged, including failures.
7. Results must survive small knob changes.
8. Weekly versus monthly rebalancing decided by the backtest after costs.

**Outputs:** daily (does anything need me), weekly (actions), monthly (is it working).

### Step 4: Agents

- LLMs only read human text. Scoring, sizing, verdicts, and explanations are code (explanations are templates filled with real numbers).
- **Panel:** fundamentals reader, news analyst, bull, bear, judge. Runs on the shortlist and every holding. Never sees the math score. Shown side by side with the math, with an agree or disagree mark.
- The panel gets a formal vote only if, across at least 30 disagreements, it was right clearly more often than the math. Bar set now, before results.
- **Models:** Claude Haiku 5.5 for routine jobs, a stronger model as judge and for catalyst reads (price to verify). Batch API for the weekly pass. $10 monthly hard cap in the console.
- **Honesty:** documents-only context, every claim carries an exact quote that code verifies, numbers come from our database, strict output schema, "not enough information" allowed, consistency reruns, explicit dates.
- **Security:** agents have no tools or write access, text is cleaned of hidden content, instruction-like text gets quarantined, documents are labeled as data, source trust tiers, sudden-swing alerts, secrets never reach agents.
- **Measurement:** a test set of 50 to 100 labeled documents (many labeled free via 8-K item numbers) decides model choice and guards every prompt change. Live tracking and monthly health stats.

### Step 5: Holdings monitor

- Trades entered by form; broker CSV upload for bulk catch-up (format depends on my broker, decided at build).
- Tracked per lot, splits and dividends applied automatically, a frozen "why I bought" snapshot, my own picks watched too, returns shown against the S&P 500, size against target.
- **Alert tiers:** urgent (red flags, catalysts, 10%+ daily drops, drawdown alarm), important (daily summary), informational (briefing).
- Hourly news, 8-K, and live-quote checks on holdings during the day.
- **Delivery:** email and Discord as equal channels. Full reports by email.
- **Noise rules:** no repeats unless worse, grouped messages, quiet hours 9 pm to 7 am Central (held until 7 am), per-holding snooze (urgent still gets through), monthly noise report.
- **Sync:** cash acquisitions closed automatically, stock deals and spin-offs flagged for me to confirm, monthly reconciliation against a broker export, deposits and withdrawals logged.

### Step 6: UI

- **Screens:** Today, Picks, Holdings (plus detail), Reports (weekly and monthly), Log trade (plus monthly check-in), Settings.
- Each screen answers one question. Phone first. Dark theme.
- Tapping Math or Agent panel on any card reveals its reasoning.
- Quiet days show follow-ups (including past calls checked honestly, misses as often as hits), holdings updates, market news, and what changed. About 10 bullets max, never a nudge to act.
- Static React + Vite + TypeScript site on Cloudflare Pages. Charts with TradingView Lightweight Charts.
- Private behind Cloudflare Access (one-time code to my email), covering pages, data files, and the trade function.
- Mockups: the "Stock App Screens" design canvas.

### Step 7: Scoreboard

- **Winning:** more return than the S&P 500 after costs, with Sharpe at least equal and worst drop no more than 1.5 times as deep.
- **Gate before real money:** the held-out test meets that definition and survives small knob changes. Otherwise paper mode and back to design, with no tweaking against the test years.
- **Paper period:** 2 to 3 months live on paper even if the gate passes.
- **Confidence:** the scoreboard says how much of a lead or lag is still within luck.
- **Kill rule**
  - Broken app (data, rules, verification, cost): pause, fix, resume.
  - Emergency brake anytime: worst drop over 1.5 times the S&P 500's and more than 10 points behind. Pause buys, full review.
  - 12 months: review only.
  - 24 months: not winning, cut the app's share in half or move to paper.
  - 36 months: not winning, stop using it for real money and move that money to an S&P 500 index fund.

### Changes after M7 started (my calls, 2026-11-05)

- **Daily picks.** Picks shows, every night, the best-scored stocks I don't hold that pass the buy rule that day, with suggested sizes and the ones new since the last trading day marked. It's for staying up to date on what's worth investing in, not for trading: we buy to hold, and sell only when a sell signal says so. The paper book still decides at the month-end close (the frozen design), so the paper record stays a test of one design.
- **Leader-dip watch (measure first, then build).** The biggest companies in industries that are booming or can boom (industries strongest over the last 12 months, plus a theme list I set, such as AI and semiconductors), when they fall about 20 to 25% from their high and may be buyable again. Not a hard limit: the idea is "down enough to be worth a look". Before building the alert, measure on 2013 to 2019 whether such dips beat the S&P 500 over the next 3, 6 and 12 months, and how often they kept falling. The numbers decide the alert's wording, or whether to build it at all. Measured 2026-11-05 (docs/DIPS.md): a dip alone isn't a buy signal. Built 2026-11-06 as a heads-up on Picks and Today, labeled with the measured odds, never "buyable".
- **Mix.** Picks cover the whole live universe, large and small. The leader-dip watch adds the big names.

## Build milestones

Each milestone has an exit check. Nothing moves on until it passes.

| # | Milestone | Done when |
|---|---|---|
| M0 | Setup: accounts, private repos, R2 bucket, secrets, spending cap | Verify list below resolved |
| M1 | Backfill: EDGAR, prices, dead companies, membership history | Quality checks pass on the full history, 30-stock source comparison reviewed |
| M2 | Daily pipeline, storage, backups | A week of clean nightly runs, first restore test passes |
| M3 | Quant engine and backtest on 2013 to 2019 | Design frozen and written down |
| M3b | One test run on 2020 to now | Gate result recorded, pass or fail |
| M4 | Agents and test set | Model choice made from test-set results |
| M5 | Holdings monitor and alerts | Alerts arrive on both channels, quiet hours respected |
| M6 | UI | All screens working on my phone behind Access |
| M7 | Paper period, 2 to 3 months | No broken-app failures in the last month |
| M8 | Real money | Started with the slice I choose |

## Verify before relying on

- Tiingo free plan keeps delisted tickers and their history
- Finnhub free tier includes the earnings calendar
- Source for splits and dividends history
- EDGAR sector codes location, and nightly rebuild of the bulk files
- Stronger model's current price for the judge role
- Whether Cloudflare Access needs an owned domain
- Gmail (or other) app-password setup for sending email
- GitHub Actions minutes measured on real runs against the free quota

## Open, decided later

- My broker (sets the CSV importer format)
- How much money the app manages (core and satellite split), changeable anytime
