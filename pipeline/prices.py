"""Daily price backfill from 2010, keyed by (symbol, date).

Sources, per security:
  yfinance  companies listed on NYSE, Nasdaq or Cboe today (a renamed company is priced under today's ticker,
            whose history covers the old one). yfinance prices, dividends and volume are split-adjusted, so raw
            values are rebuilt from its split table (PLAN rule 9).
  tiingo    S&P securities that no longer trade. Tiingo returns raw prices, dividends and split factors.
  wiki      stretches Tiingo lacks, from Nasdaq Data Link's WIKI Prices (raw, frozen 2018-03-27).
Stretches no source covers go to market/price_gaps.parquet: unpriced, never guessed.

A price series follows a ticker, which can pass between companies over time. Rows are attributed to a CIK
through the dated ticker map (market/ticker_map.parquet), not here.

Usage: python -m pipeline.prices OUT_DIR plan|yfinance|tiingo|wiki|build
  plan       market/price_plan.parquet: source and symbol for each stretch of each index security, plus the
             common stock of every other listed company (the live universe) on yfinance
  yfinance   raw/yfinance/<date>/batch_NNN.parquet
  tiingo     raw/tiingo/<SYMBOL>.json, one per symbol, skipped if already there (checkpoint)
  wiki       raw/wiki/<SYMBOL>.json, same
  build      from the raw files: market/prices/year=YYYY/ (all rows, every symbol), market/price_map.parquet
             (which symbol's rows are valid for which index security and dates) and market/price_gaps.parquet.
             Use prices through price_map: a symbol's rows outside its validated stretches may be another
             company that later took the ticker.

Rerunning after a build re-plans around rejected symbols (a series that starts too late is another security).
"""
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import requests

SINCE = "2010-01-01"
WIKI_END = "2018-03-27"
SLACK = 10  # days a source may stop short of a stretch's end: index removal comes a few days after the last trade
TIINGO_LIST_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
TIINGO_MONTHLY_SYMBOLS = 450  # free plan allows 500 unique symbols a month; leave room for the spot check
ALIASES = Path(__file__).with_name("price_aliases.csv")
EXCHANGES = ("NYSE", "Nasdaq", "CBOE")


def shift(date, days):
    return (pd.Timestamp(date) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def common_tickers(c):
    """A company's common-stock tickers in '.' form (BRK.B). Drops preferreds and units (BAC-PL, X-WT) and
    Nasdaq 5-letter warrants, rights and units (ending W, R, U)."""
    out = []
    for t in filter(None, str(c["tickers"]).split(",")):
        if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", t) and not (len(t) == 5 and t[-1] in "WRU"):
            out.append(t.replace("-", "."))
    return out


def listed(c):
    return any(e in EXCHANGES for e in str(c["exchanges"]).split(",")) and bool(common_tickers(c))


def security_spells(segs):
    """One row per security: a ticker spell, split where a new_company segment begins (21st Century Fox's FOXA
    and Fox Corp's FOXA are two securities)."""
    out = []
    for (ticker, _), g in segs.sort_values("start").groupby(["ticker", "spell_start"]):
        run = None
        for r in g.itertuples():
            end = r.end if isinstance(r.end, str) else None
            if run and r.link != "new_company":
                run.update(end=end, cik=r.cik)
            else:
                if run:
                    out.append(run)
                run = {"label": ticker, "start": r.start, "end": end, "cik": r.cik}
        out.append(run)
    return out


def plan(segs, companies, tiingo, aliases, rejected=frozenset()):
    """Pieces (label, need_from, need_to, source, symbols, index) covering each index security since 2010, then
    the other listed companies' common stock on yfinance. `rejected` holds (label, source, symbol) combinations
    an earlier build found to be another security (series starting too late); they're skipped."""
    co = companies.set_index("cik", drop=False)
    tl = tiingo[(tiingo["assetType"] == "Stock") & (tiingo["priceCurrency"] == "USD")].assign(
        T=lambda x: x["ticker"].str.upper().str.replace("-", "."))
    # Tiingo's API serves only the newest listing on a ticker (APC is today's company, not Anadarko), so an
    # older entry in its list can't be fetched
    tl = tl[tl["startDate"].notna()].sort_values("startDate").groupby("T").tail(1)
    rows, done = [], set()
    later_tickers = segs.sort_values("start").groupby("cik")["ticker"].apply(lambda t: list(dict.fromkeys(t))).to_dict()

    def piece(label, frm, to, source, symbols, cik, index=True, late=False):
        """late: the source's history starts after this security was already trading (Tiingo cut at 2016, or
        another company that took the ticker), so identity has to be proven, not just not disproven."""
        rows.append({"label": label, "need_from": frm, "need_to": to, "source": source,
                     "symbols": "|".join(symbols), "cik": cik, "index": index, "late": late})

    for s in security_spells(segs):
        if s["end"] is not None and s["end"] < SINCE:
            continue
        frm, to, cik = max(s["start"], SINCE), s["end"], s["cik"]
        c = co.loc[int(cik)] if pd.notna(cik) and int(cik) in co.index else None
        cur = common_tickers(c) if c is not None and listed(c) else []
        # still listed: its own ticker, else today's ticker when there's only one (FB -> META), else the
        # company's other tickers (PSKY now trades as SKYD), skipping any an earlier build rejected
        yf_cands = ([s["label"]] + [t for t in cur if t != s["label"]]) if s["label"] in cur else \
            (cur if len(cur) == 1 else [])
        yf_cands = [t for t in yf_cands if (s["label"], "yfinance", t) not in rejected]
        if yf_cands:
            piece(s["label"], frm, to, "yfinance", yf_cands[:1], cik)
            done.add(int(cik))
            continue
        # no longer trades (or today's ticker is another security): Tiingo first, using the tickers it traded
        # under, then the same company's later tickers (Tiingo files Cabot Oil & Gas's COG history under CTRA),
        # then a bankruptcy 'Q' ticker
        later = [t for t in later_tickers.get(cik, []) if t != s["label"]]
        cands = list(dict.fromkeys(aliases.get(s["label"], []) + [s["label"]] + later + [s["label"] + "Q"]))
        to_ = to or today()
        best, best_overlap = None, 0
        for e in tl[tl["T"].isin(cands)].itertuples():
            if not isinstance(e.endDate, str) or (s["label"], "tiingo", e.ticker) in rejected:
                continue
            lo, hi = max(frm, e.startDate), min(to_, e.endDate)
            days = (pd.Timestamp(hi) - pd.Timestamp(lo)).days if lo <= hi else 0
            if days > best_overlap:
                best, best_overlap = e, days
        rest = [(frm, to_)]
        if best is not None:
            lo = frm if best.startDate <= shift(frm, SLACK) else best.startDate
            hi = to_ if best.endDate >= shift(to_, -SLACK) else best.endDate
            piece(s["label"], lo, hi if hi != to_ else to, "tiingo", [best.ticker], cik, late=lo > frm)
            rest = [(a, b) for a, b in [(frm, lo), (hi, to_)] if (pd.Timestamp(b) - pd.Timestamp(a)).days > SLACK]
        for a, b in rest:  # WIKI for what Tiingo lacks, up to its 2018 freeze; after that it stays a gap
            wiki = [t for t in cands if t != s["label"] + "Q" and (s["label"], "wiki", t) not in rejected]
            if a < WIKI_END and wiki:
                piece(s["label"], a, min(b, WIKI_END), "wiki", wiki, cik)
            elif a < WIKI_END:
                piece(s["label"], a, min(b, WIKI_END), "none", [], cik)
            if b > WIKI_END:
                piece(s["label"], max(a, WIKI_END), b, "none", [], cik)
    for c in companies.itertuples():  # live universe: every other listed company's common stock
        if c.cik in done or not listed(companies.loc[c.Index]):
            continue
        for t in common_tickers(companies.loc[c.Index]):
            piece(t, SINCE, None, "yfinance", [t], c.cik, index=False)
    return pd.DataFrame(rows)


def rebuild_raw(h):
    """yfinance history (split-adjusted) -> raw OHLCV and dividends. F = product of the split ratios after each
    day; prices and dividends times F, volume divided by F. Returns a frame with split = ratio on its day."""
    ratio = h["Stock Splits"].replace(0, 1.0).fillna(1.0)
    f = ratio[::-1].cumprod()[::-1] / ratio  # ratios strictly after each day
    return pd.DataFrame({
        "open": h["Open"] * f, "high": h["High"] * f, "low": h["Low"] * f, "close": h["Close"] * f,
        "volume": h["Volume"] / f, "dividend": h["Dividends"].fillna(0) * f, "split": ratio,
    }, index=h.index)


def fetch_yfinance(out, symbols, batch=100):
    """Raw batches into raw/yfinance/<today>/. Symbols already fetched today are skipped (a second pass only
    fetches what the re-plan added)."""
    import yfinance as yf
    d = out / "raw" / "yfinance" / today()
    d.mkdir(parents=True, exist_ok=True)
    have = set().union(*[set(pd.read_parquet(p, columns=["symbol"])["symbol"]) for p in d.glob("batch_*.parquet")])
    symbols = [s for s in symbols if s not in have]
    first = len(list(d.glob("batch_*.parquet")))
    print(f"yfinance: {len(symbols)} symbols to fetch, {len(have)} already fetched today", flush=True)
    failed = []
    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        yahoo = [s.replace(".", "-") for s in chunk]
        w = yf.download(yahoo, start=SINCE, auto_adjust=False, actions=True, group_by="ticker",
                        threads=True, progress=False)
        long = w.stack(level=0, future_stack=True).rename_axis(["date", "symbol"]).reset_index()
        long["symbol"] = long["symbol"].str.replace("-", ".")
        empty = [s for s in chunk if long.loc[long["symbol"] == s, "Close"].notna().sum() == 0]
        for s in empty:  # one retry each, alone
            h = yf.Ticker(s.replace(".", "-")).history(start=SINCE, auto_adjust=False, actions=True)
            if len(h):
                h = h.rename_axis("date").reset_index().assign(symbol=s)
                long = pd.concat([long[long["symbol"] != s], h], ignore_index=True)
            else:
                failed.append(s)
        long["date"] = pd.to_datetime(long["date"], utc=True).dt.strftime("%Y-%m-%d")
        long.to_parquet(d / f"batch_{first + i // batch:03d}.parquet", index=False)
        print(f"yfinance batch {i // batch + 1}/{-(-len(symbols) // batch)}: {len(chunk) - len(empty)} ok, "
              f"{len(empty)} retried, {len(failed)} failed so far", flush=True)
        time.sleep(2)
    return failed


def fetch_tiingo(out, symbols, folder="tiingo"):
    """Raw Tiingo history per symbol into raw/<folder>/<SYMBOL>.json, skipping files already there."""
    d = out / "raw" / folder
    d.mkdir(parents=True, exist_ok=True)
    todo = [s for s in symbols if not (d / f"{s}.json").exists()]
    print(f"tiingo: {len(symbols)} symbols, {len(todo)} to fetch at about 50 an hour", flush=True)
    for n, s in enumerate(todo):
        for attempt in range(4):
            r = requests.get(f"https://api.tiingo.com/tiingo/daily/{s}/prices", params={"startDate": SINCE},
                             headers={"Authorization": f"Token {os.environ['TIINGO_API_KEY']}"}, timeout=120)
            if r.status_code in (200, 404):
                break
            print(f"tiingo {s}: HTTP {r.status_code} (try {attempt + 1}/4)", file=sys.stderr, flush=True)
            time.sleep(120 * (attempt + 1))
        else:
            r.raise_for_status()
        body = r.json() if r.status_code == 200 else {"error": r.status_code}
        (d / f"{s}.json").write_text(json.dumps(body))  # raw first; also the checkpoint
        print(f"tiingo {n + 1}/{len(todo)} {s}: {len(body) if isinstance(body, list) else body}", flush=True)
        time.sleep(75)  # free plan: 50 requests an hour


def fetch_wiki(out, symbols):
    d = out / "raw" / "wiki"
    d.mkdir(parents=True, exist_ok=True)
    for s in [s for s in symbols if not (d / f"{s}.json").exists()]:
        rows, cols, cursor = [], None, None
        while True:
            q = {"ticker": s, "api_key": os.environ["NASDAQ_DATA_LINK_KEY"]}
            if cursor:
                q["qopts.cursor_id"] = cursor
            r = requests.get("https://data.nasdaq.com/api/v3/datatables/WIKI/PRICES.json", params=q, timeout=120)
            r.raise_for_status()
            body = r.json()
            cols = [c["name"] for c in body["datatable"]["columns"]]
            rows += body["datatable"]["data"]
            cursor = body["meta"].get("next_cursor_id")
            time.sleep(0.5)
            if not cursor:
                break
        (d / f"{s}.json").write_text(json.dumps({"columns": cols, "data": rows}))
        print(f"wiki {s}: {len(rows)} rows", flush=True)


COLS = ["symbol", "source", "date", "open", "high", "low", "close", "volume", "dividend", "split", "fetched", "year"]


def raw_batches(out):
    """Yields frames (symbol, source, date, open..split, fetched) from the raw files, one yfinance batch at a time
    and then all of Tiingo and WIKI, so the full history never has to sit in memory at once."""
    ydirs = sorted((out / "raw" / "yfinance").glob("????-??-??"))
    for p in sorted(ydirs[-1].glob("batch_*.parquet")) if ydirs else []:
        y = pd.read_parquet(p).dropna(subset=["Close"])
        parts = [rebuild_raw(g.set_index("date").sort_index()).rename_axis("date").reset_index().assign(
            symbol=sym, source="yfinance", fetched=ydirs[-1].name) for sym, g in y.groupby("symbol")]
        if parts:
            yield pd.concat(parts, ignore_index=True)
    parts = []
    for p in (out / "raw" / "tiingo").glob("*.json"):
        body = json.loads(p.read_text())
        if isinstance(body, list) and body:
            t = pd.DataFrame(body)
            parts.append(pd.DataFrame({"date": t["date"].str[:10], "open": t["open"], "high": t["high"],
                                       "low": t["low"], "close": t["close"], "volume": t["volume"],
                                       "dividend": t["divCash"], "split": t["splitFactor"]}).assign(
                symbol=p.stem, source="tiingo", fetched=pd.Timestamp(p.stat().st_mtime, unit="s").strftime("%Y-%m-%d")))
    for p in (out / "raw" / "wiki").glob("*.json"):
        body = json.loads(p.read_text())
        if body["data"]:
            w = pd.DataFrame(body["data"], columns=body["columns"])
            parts.append(pd.DataFrame({"date": w["date"], "open": w["open"], "high": w["high"], "low": w["low"],
                                       "close": w["close"], "volume": w["volume"], "dividend": w["ex-dividend"],
                                       "split": w["split_ratio"]}).assign(symbol=p.stem, source="wiki", fetched="2018-03-27"))
    if parts:
        yield pd.concat(parts, ignore_index=True)


def build(out):
    """Write market/prices/year=YYYY/ batch by batch. Returns (rows, {(source, symbol): (first, last date)})."""
    import shutil
    dest = out / "market" / "prices"
    shutil.rmtree(dest, ignore_errors=True)  # a full rebuild: rerunning gives the same files (idempotent)
    n, span = 0, {}
    for i, df in enumerate(raw_batches(out)):
        df = df[df["date"] >= SINCE].assign(year=lambda x: x["date"].str[:4].astype(int))
        ds.write_dataset(pa.Table.from_pandas(df[COLS], preserve_index=False), dest, format="parquet",
                         partitioning=["year"], partitioning_flavor="hive", basename_template=f"b{i:03d}-{{i}}.parquet",
                         existing_data_behavior="overwrite_or_ignore")
        for k, g in df.groupby(["source", "symbol"])["date"]:
            span[k] = (g.min(), g.max())
        n += len(df)
    return n, span


def coverage(plan_df, span, last_day):
    """Validate each index piece against the data. span: {(source, symbol): (first, last)}.

    A piece's symbols are tried in order (WPI, then ACT). A symbol counts from where coverage stands only if its
    series has started by then (within SLACK days). A series that starts later is another security on the
    same ticker (Tiingo's PARA is Banzai, not ViacomCBS) and is rejected outright, never used in part.

    Returns (price_map: label, cik, source, symbol, from, to for validated stretches;
             gaps: label, cik, source, symbols, gap_from, gap_to, reason, rejected)."""
    pm, gp = [], []
    for r in plan_df[plan_df["index"]].itertuples():
        to = r.need_to if isinstance(r.need_to, str) else last_day
        cur, used, rejected = r.need_from, False, []
        for s in filter(None, r.symbols.split("|")):
            if (r.source, s) not in span:
                continue
            first, last = span[(r.source, s)]
            if last <= cur:
                continue  # ends before where we are: an earlier listing, irrelevant
            if first > shift(cur, SLACK):
                rejected.append(s)
                continue
            pm.append({"label": r.label, "cik": r.cik, "source": r.source, "symbol": s, "from": cur,
                       "to": min(last, to) if last < shift(to, -SLACK) else r.need_to,
                       "late": bool(getattr(r, "late", False))})
            cur, used = last, True
            if last >= shift(to, -SLACK):
                break
        if cur < shift(to, -SLACK):
            reason = "no source" if r.source == "none" else "ends early" if used else \
                "other security on ticker" if rejected else "no data"
            gp.append({"label": r.label, "cik": r.cik, "source": r.source, "symbols": r.symbols,
                       "gap_from": cur, "gap_to": to, "reason": reason, "rejected": "|".join(rejected)})
    cols = ["label", "cik", "source", "symbols", "gap_from", "gap_to", "reason", "rejected"]
    return pd.DataFrame(pm), pd.DataFrame(gp, columns=cols)


def apply_identity(out, pm, g, last_day):
    """Run pipeline.identity on the validated stretches. A Tiingo or WIKI stretch that fails becomes a gap, and
    its symbol is rejected for the next plan. A yfinance stretch is the listed company's own ticker by
    construction, so a failure there is kept and listed for review: it points at the check (share classes,
    say) rather than at the source."""
    from pipeline.identity import check
    facts = out / "market" / "fundamentals" / "facts.parquet"
    if not facts.exists():
        sys.exit(f"{facts} is missing: run the backfill workflow first")
    segs = pd.read_parquet(out / "market" / "ticker_map.parquet")
    idv = check(pm, out / "market" / "prices", facts, segs, last_day)
    vendor = idv["source"].isin(["tiingo", "wiki"])
    late = idv["late"].fillna(False).astype(bool) if "late" in idv else False
    drop = vendor & ((idv["verdict"] == "fail") | (late & (idv["verdict"] != "pass")))
    why = lambda r: ("identity check failed: " + ", ".join(
        k for k in ["shared", "dividends", "float"] if (bool(r[k]) if k == "shared" else r[k] == "fail"))
        if r["verdict"] == "fail" else "late-starting series not proven to be this company")
    new_gaps = pd.DataFrame([{"label": r["label"], "cik": r["cik"], "source": r["source"], "symbols": r["symbol"],
                              "gap_from": r["from"], "gap_to": r["to"] if isinstance(r["to"], str) else last_day,
                              "reason": why(r), "rejected": r["symbol"]} for _, r in idv[drop].iterrows()],
                            columns=g.columns)
    keep = pm.reset_index(drop=True)[~drop.values]
    return keep, pd.concat([g, new_gaps], ignore_index=True), idv


def main(out_dir, step):
    out = Path(out_dir)
    if step == "plan":
        p = out / "raw" / "tiingo_list" / today() / "supported_tickers.zip"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(requests.get(TIINGO_LIST_URL, timeout=300).content)  # raw first
        z = zipfile.ZipFile(p)
        tiingo = pd.read_csv(io.TextIOWrapper(z.open(z.namelist()[0])), dtype=str)
        al = pd.read_csv(ALIASES, dtype=str, comment="#").groupby("label")["ticker"].apply(list).to_dict()
        prev = out / "market" / "price_gaps.parquet"  # symbols an earlier build found to be another security
        rejected = set()
        if prev.exists():
            g = pd.read_parquet(prev)
            if "rejected" in g:
                rejected = {(r.label, r.source, s) for r in g.itertuples() for s in str(r.rejected).split("|") if s}
        print(f"plan: {len(rejected)} (security, source, symbol) combinations rejected by the last build")
        pl = plan(pd.read_parquet(out / "market" / "ticker_map.parquet"),
                  pd.read_parquet(out / "market" / "companies.parquet"), tiingo, al, rejected)
        pl.to_parquet(out / "market" / "price_plan.parquet", index=False)
        print(pl.groupby(["index", "source"]).size().to_string())
        return
    pl = pd.read_parquet(out / "market" / "price_plan.parquet")
    syms = lambda src: sorted({s for x in pl.loc[pl["source"] == src, "symbols"] for s in x.split("|") if s})
    if step == "yfinance":
        failed = fetch_yfinance(out, syms("yfinance"))
        idx = set(pl.loc[pl["index"] & (pl["source"] == "yfinance"), "symbols"])
        print(f"yfinance: {len(failed)} symbols returned nothing; index securities among them: "
              f"{sorted(set(failed) & idx)}; others: {failed[:30]}")
        if set(failed) & idx:
            sys.exit("index securities without yfinance data")
    elif step == "tiingo":
        s = syms("tiingo")
        if len(s) > TIINGO_MONTHLY_SYMBOLS:
            sys.exit(f"{len(s)} Tiingo symbols is over the {TIINGO_MONTHLY_SYMBOLS} monthly budget")
        fetch_tiingo(out, s)
    elif step == "wiki":
        fetch_wiki(out, syms("wiki"))
    elif step == "build":
        n, span = build(out)
        last_day = max((v[1] for (src, _), v in span.items() if src == "yfinance"), default=today())
        pm, g = coverage(pl, span, last_day)
        pm, g, idv = apply_identity(out, pm, g, last_day)
        pm.to_parquet(out / "market" / "price_map.parquet", index=False)
        g.to_parquet(out / "market" / "price_gaps.parquet", index=False)
        idv.to_parquet(out / "market" / "price_identity.parquet", index=False)
        (out / "review").mkdir(parents=True, exist_ok=True)
        g.to_csv(out / "review" / "price_gaps.csv", index=False)
        vendor = idv["source"].isin(["tiingo", "wiki"])
        idv[vendor & (idv["verdict"] == "unverifiable")].to_csv(out / "review" / "price_unverified.csv", index=False)
        idv[~vendor & (idv["verdict"] == "fail")].to_csv(out / "review" / "price_identity_yfinance_fails.csv", index=False)
        per_source = pd.Series([src for src, _ in span]).value_counts().to_dict()
        print(f"prices: {n} rows, symbols per source {per_source}, last day {last_day}")
        print("identity verdicts:", idv.groupby(["source", "verdict"]).size().to_dict())
        print(f"validated index stretches: {len(pm)}; without prices: {len(g)} "
              f"({g['reason'].value_counts().to_dict()})")
        if len(g):
            print(g[["label", "source", "symbols", "gap_from", "gap_to", "reason", "rejected"]].to_string(index=False))
    else:
        sys.exit(f"unknown step {step}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
