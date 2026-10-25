# M0: Verify before relying on

Checked 2026-10-25. Each item: result, evidence, sources. **PASS** = confirmed. **FAIL** = the plan's assumption doesn't hold, needs a decision. **PARTIAL** = confirmed as far as I can go without a key or account I don't have.

| # | Item | Result |
|---|---|---|
| 1 | Tiingo free plan keeps delisted tickers and their history | PARTIAL, with a catch |
| 2 | Finnhub free tier includes the earnings calendar | PASS (limited). Finnhub price fallback FAILED, replaced by Tiingo (decided) |
| 3 | Source for splits and dividends history | Decided: Tiingo for history, yfinance for daily |

## Decisions

| Date | Decision | Why |
|---|---|---|
| 2026-10-25 | **Tiingo EOD replaces Finnhub as the daily price fallback** and is the second source for the cross-source spot check. The spot check draws from a **fixed pool of about 150 symbols, rotated monthly**, instead of 20 to 30 random stocks a day. | Finnhub daily candles are premium-only (item 2). Random daily picks would use about 660 unique symbols a month, over Tiingo's 500-symbol free cap, which the dead-company backfill also needs. |
| 2026-10-25 | **Splits and dividends: Tiingo `divCash`/`splitFactor` for history (including dead companies), yfinance `actions` for the daily update on live names.** Each cross-checks the other where both exist. | Finnhub splits and dividends are premium. yfinance returns nothing for delisted names. Tiingo returns both fields in the same response as backfill prices (item 3). |

Budget note for M1: Tiingo's 500 unique symbols a month are shared by the dead-company backfill and the spot-check pool. Run the backfill first, then start the pool.
| 4 | EDGAR sector codes location, nightly bulk-file rebuild | PASS |
| 5 | Stronger model's current price for the judge role | PASS |
| 6 | Whether Cloudflare Access needs an owned domain | PASS (no domain needed) |
| 7 | Gmail app-password setup | PASS (docs), needs you to do it |
| 8 | GitHub Actions minutes on real runs | Not measurable yet, estimate below |

---

## 1. Tiingo: delisted tickers and history: PARTIAL

**Test made:** downloaded Tiingo's public ticker list (`supported_tickers.zip`, no key needed, 109,127 rows, columns `ticker, exchange, assetType, priceCurrency, startDate, endDate`) and looked up known dead S&P 500 names.

**Acquired companies are there under their old ticker:**

| Ticker | Exchange | Start | End |
|---|---|---|---|
| TWTR | NYSE | 2013-11-07 | 2022-10-28 |
| CELG | NASDAQ | 1990-03-26 | 2019-11-22 |
| XLNX | NASDAQ | 1990-06-18 | 2022-02-14 |
| ATVI | NASDAQ | 1993-10-25 | 2023-10-13 |

**Failed companies are there too, but only under their post-bankruptcy OTC ticker,** with the history starting at the original listing:

| Company | Old ticker | Found in Tiingo as | Exchange | Start | End |
|---|---|---|---|---|---|
| SVB Financial | SIVB | SIVBQ | PINK | 1990-03-26 | still open |
| First Republic | FRC | FRCB | PINK | 2010-12-09 | still open |
| Signature Bank | SBNY | SBNY | PINK | 2004-03-23 | still open |
| Bed Bath & Beyond | BBBY | BBBYQ | PINK | 1992-06-05 | 2023-09-29 |
| Rite Aid | RAD | RADCQ | PINK | 1984-12-17 | still open |

Looking up `SIVB`, `FRC`, or `BBBY` directly finds nothing. Tickers are also reused (two separate `DO` rows, two `APC` rows), which the plan already handles with date-ranged mappings.

**What this means:** the data exists, but the backfill can't just loop over old S&P 500 tickers. Failed names need a mapping step (old ticker → Tiingo ticker), probably by company name plus date overlap. Tiingo's list has no CIK, so this is manual-ish work for the failures. That's M1 work, not a blocker.

**Not yet tested:** an actual price-history call for a dead ticker (needs a Tiingo API key). One call each for `TWTR` and `SIVBQ` would close this item.

**Free plan limits** (pricing page): 500 unique symbols/month, 50 requests/hour, 1,000 requests/day, 1 GB/month, 30+ years of price history, license "Internal Use Only" (personal use, no sharing). The 500-symbol cap is shared with any other Tiingo use (see item 2).

Sources: [Tiingo EOD docs](https://www.tiingo.com/documentation/end-of-day), [Tiingo pricing](https://www.tiingo.com/about/pricing), `https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip`

## 2. Finnhub free tier: earnings calendar PASS, price fallback FAIL

Read from Finnhub's own API spec (`https://finnhub.io/static/swagger.json`), which carries per-endpoint `freeTier` and `premium` fields:

| Endpoint | Plan use | Free? |
|---|---|---|
| `/calendar/earnings` | Earnings dates | **Yes**, "1 month of historical earnings and new updates" |
| `/company-news` | News headlines | **Yes**, "1 year of historical news and new updates" |
| `/quote` | Live quotes for holdings | **Yes** |
| `/stock/candle` | **Daily price fallback** | **No**, "Premium Access Required" |
| `/stock/split` | Splits | **No**, "Premium required." |
| `/stock/dividend`, `/stock/dividend2` | Dividends | **No**, premium |

The earnings calendar covers upcoming dates fine. One month of history is enough for timing and the briefing. The earnings-surprise signal uses EDGAR fundamentals, so it doesn't depend on this.

**FAIL: the plan lists "yfinance, Finnhub fallback" for daily prices. Finnhub daily candles are premium-only, so that fallback doesn't exist on the free tier.**

**Proposed alternative:** use Tiingo EOD as the second price source, both as the fallback and for the daily 20 to 30 stock cross-source spot check. **Catch:** 30 *random* stocks a day is about 660 unique symbols a month, over Tiingo's 500 cap, and the cap is also needed for the dead-company backfill. Fix: draw the spot check from a fixed pool of about 150 symbols that rotates monthly, and run the dead-company backfill first. **Approved 2026-10-25** (see Decisions).

Source: [Finnhub earnings calendar docs](https://finnhub.io/docs/api/earnings-calendar) (rendered from the spec above)

## 3. Splits and dividends history: decided

Tested:

- **yfinance 1.7.0** `Ticker.actions` works for listed names (AAPL: 97 dividend/split events, NVDA: 62, latest 2026-09-10). It returns **empty for delisted names** (TWTR, SIVB: "possibly delisted").
- **Finnhub** splits and dividends are premium (item 2).
- **Tiingo EOD** returns `divCash` (dividend on its ex-date) and `splitFactor` on every daily row, raw and adjusted prices side by side. It covers dead companies (item 1). Not yet called (needs key).

**Proposal:** Tiingo `divCash`/`splitFactor` is the source for history, including dead names, since it comes in the same response as the backfill prices. yfinance `actions` handles the daily update for live names. Each one cross-checks the other where both exist. Rule 9 of the plan (raw prices plus our own adjustment math) works with both. **Approved 2026-10-25** (see Decisions).

Sources: [Tiingo EOD docs](https://www.tiingo.com/documentation/end-of-day), local test with yfinance 1.7.0

## 4. EDGAR sector codes and bulk files: PASS

**Sector codes:** the submissions JSON has `sic` and `sicDescription`, plus an `ownerOrg` field naming the SEC review office. Test call `https://data.sec.gov/submissions/CIK0000320193.json` returned `sic: 3571`, `sicDescription: Electronic Computers`, `ownerOrg: 06 Technology`, `exchanges: [Nasdaq]`. The same fields are in `submissions.zip`.

Two caveats for M1:

- SIC is **current only**, not point-in-time. A company that changed industry carries today's code back through history. Small effect, but worth knowing for sector-relative ranks.
- SIC is about 440 codes, not sectors. We need our own SIC → sector map, either SIC divisions or the `ownerOrg` office (about 10 groups).

**Bulk files:** SEC says the bulk ZIPs are "recompiled nightly", "at approximately 3:00 a.m. ET". HEAD requests today (Sunday) showed:

| File | Size | Last-Modified |
|---|---|---|
| `submissions.zip` | 1.57 GB | Fri 2026-10-23 23:55 GMT |
| `companyfacts.zip` | 1.41 GB | Fri 2026-10-23 23:48 GMT |

So the real build time is about 7:50 pm ET, not 3 am, and there were no weekend rebuilds. The nightly job (about 7:17 pm Central = 8:17 pm ET) runs after that, which is fine. The pipeline should check `Last-Modified` and not assume a time. About 3 GB a night is too much to download every day, so use the per-company JSON APIs for daily updates and the ZIPs for backfill.

**Fair access:** "Current max request rate: 10 requests/second", User-Agent must declare a company/name and admin email. That matches the plan. The real User-Agent contact should live in a GitHub secret or variable.

Sources: [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), [Accessing EDGAR data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

## 5. Judge model price: PASS

Official pricing, per million tokens:

| Model | Input | Output | Batch input | Batch output |
|---|---|---|---|---|
| Claude Haiku 5.5 (prompts ≤100K tokens) | $0.10 | $0.50 | $0.05 | $0.25 |
| Claude Haiku 5.5 (prompts >100K tokens) | $0.50 | $2.50 | $0.25 | $1.25 |
| Claude Sonnet 5.5 | $2 | $10 | $1 | $5 |
| Claude Opus 5.5 | $4 | $20 | $2 | $10 |

Batch is 50% off input and output, and it stacks with prompt caching.

**Rough budget check** (my estimate, not measured): a weekly judge pass on about 40 stocks at about 20K input / 2K output each, on Opus 5.5 batch, is 0.8M in + 0.08M out = about $2.40/week, about $10/month. **That alone hits the cap.** Sonnet 5.5 batch is about $1.20/week, about $5/month. Haiku 5.5 for the routine panel roles is cents. So the judge is probably Sonnet 5.5, with Opus 5.5 kept for the rare catalyst deep reads. The M4 test set decides, as planned. Also keep Haiku prompts under 100K tokens, since the price jumps 5x above that.

Source: [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing)

## 6. Cloudflare Access without an owned domain: PASS

No owned domain is needed for either piece:

- **Pages:** the project's "Enable access policy" setting protects preview deployments only. Cloudflare's Pages known-issues page documents extra steps to also protect the production `*.pages.dev` hostname (edit the Access app's hostname to remove the wildcard, then re-enable the policy so there are two apps). No custom domain required.
- **Worker:** Access can protect "every domain associated with the Worker", including its `workers.dev` hostname. No custom domain required.
- A *generic* self-hosted Access app does need "an active domain on Cloudflare". We avoid that path by using the Pages and Worker built-in toggles.

So the $10/year domain line in the budget can likely be dropped. To confirm at M6: after setup, open the production `*.pages.dev` URL in a private window and make sure it asks for the one-time code. Third-party guides warn that a missed hostname stays public.

Sources: [Pages preview deployments](https://developers.cloudflare.com/pages/configuration/preview-deployments/), [Pages known issues](https://developers.cloudflare.com/pages/platform/known-issues/), [Workers + Access](https://developers.cloudflare.com/workers/configuration/cloudflare-access/), [Self-hosted Access app](https://developers.cloudflare.com/cloudflare-one/applications/configure-apps/self-hosted-public-app/)

## 7. Gmail app password: PASS (docs), needs you

- 2-Step Verification is required.
- The option is hidden if 2SV uses security keys only, on work/school accounts, and with Advanced Protection.
- Create at `myaccount.google.com/apppasswords`.
- **Changing the Google password revokes all app passwords**, so sending would silently break. The pipeline must treat an SMTP auth failure as a loud failure, and a Discord alert covers that case.
- SMTP: `smtp.gmail.com`, port 587 (STARTTLS). This is standard Gmail SMTP and isn't stated on the page cited.

Can't test without your account. **You:** pick the sending account (a dedicated Gmail is safer than your main one), create the app password, and add it as a GitHub secret. Test at M5.

Source: [Google: Sign in with app passwords](https://support.google.com/accounts/answer/185833)

## 8. GitHub Actions minutes: not measurable yet

Real jobs don't exist until M2. Facts: GitHub Free gives **2,000 minutes/month for private repos**, Linux runner $0.006/min after that, public repos free, 500 MB artifact storage. Without a payment method on file, jobs are **blocked** when the quota runs out, so they stop rather than charge you.

**Estimate** (to replace with measured numbers at M2):

| Job | Runs/month | Guess min/run | Minutes |
|---|---|---|---|
| Holdings watch (hourly, ~11/day, weekdays) | ~240 | 2 | ~480 |
| Nightly | ~22 | 15 | ~330 |
| Weekly | ~4 | 15 | ~60 |
| Monthly | 1 | 30 | 30 |
| **Total** | | | **~900 of 2,000** |

Fits, but the hourly job dominates. Python setup time per run matters, so cache dependencies. The R2 check workflow (task 3) will give the first real number for setup overhead.

Source: [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

---

## Found along the way (not on the list)

- **`StockWatchBackups` repo is PUBLIC.** The plan says the backup repo is private, and it will hold trades and holdings. Make it private before anything is written to it.
- **gh CLI account:** resolved. `poojan-9603` is now the active gh account, and `StockWatch` is confirmed private.

## R2 check workflow (task 3): PASS

- Run [40233146564](https://github.com/poojan-9603/StockWatch/actions/runs/40233146564) (2026-10-25) failed with `secret for R2_ACCOUNT_ID is missing or empty`, because the repo had no secrets yet. That confirmed the fail-loud path.
- After the four secrets were added, run [40233924222](https://github.com/poojan-9603/StockWatch/actions/runs/40233924222) (2026-10-25) **passed**. It wrote `logs/m0-r2-check/<run-id>.txt`, read it back byte-identical, and deleted it. Write took 1.4 s, read 0.8 s.
- The run log shows all four secrets as `***`. No values leaked.
- First real timing: the whole job took about 11 seconds, which bills as 1 minute.
- **yfinance from GitHub's data-center IPs** is sometimes rate-limited or blocked by Yahoo. It worked from this machine. It's untested from Actions until M1, and it's another reason the Tiingo fallback matters.
