"""Backtest on the 2013 to 2019 tuning years. quant.lock keeps every date from 2020 on out until the user unlocks it.

PLAN Step 3 rules: decide at a rebalance day's close and trade at the next day's open; fundamentals count from their
filing date; the universe is the S&P 500 as of each day (removed and dead companies included); 0.1% cost per trade;
an acquired company exits at its final price and a failed one 30% below it (failed: an 8-K item 1.03, bankruptcy,
within a year before its prices stop); benchmarks SPY (S&P 500 with dividends) and RSP (equal-weighted).

Prices come only from validated stretches (market/price_map.parquet). A company can't be bought on a quarantined
day (market/quality/issues.parquet). A holding whose prices stop exits at its last close (docs/M3.md).

Usage: python -m quant.backtest OUT_DIR bench|signals|run
  bench    raw/bench/<date>/ and bench/{SPY,RSP}.parquet, bench/vix.parquet (yfinance, FRED VIXCLS)
  signals  quant/raw.parquet: every candidate's raw value at every rebalance day; review/m3_ic.csv,
           review/m3_eligible.csv
  run      every variation in variations() -> review/m3_backtest.csv and inbox/experiments/*.json
"""
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as ds
import requests

from quant import lock, signals as sg
from quant.measure import members
from quant.sectors import gics11

START = "2013-01-31"
FAILED_PENALTY = 0.30
BASE = dict(rebalance="weekly", choice={"quality": "roa", "value": "bm"}, rule="mean", buy=80.0, n60=5, sell=40.0,
            strikes=2, max_pos=20, avg=1 / 15, min_w=0.02, cap_x=1.5, cap=0.15, sector_cap=0.30, swap=20.0,
            cost=0.001, filter="soft", vix=20.0, soft_raise=5.0, soft_size=0.5, timing=False, nudge=False, stop=False)


def end_day():
    return min(lock.cutoff(), datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def retry(f, tries=4):
    for i in range(tries):
        try:
            return f()
        except Exception as e:  # noqa: BLE001  retry, then fail loudly (rule 5)
            if i == tries - 1:
                raise
            print(f"retry {i + 1}: {e}", flush=True)
            time.sleep(10 * 2 ** i)


def bench(out):
    import yfinance as yf
    from pipeline.prices import rebuild_raw
    raw = out / "raw" / "bench" / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw.mkdir(parents=True, exist_ok=True)
    (out / "bench").mkdir(exist_ok=True)
    for sym in ["SPY", "RSP"]:
        h = retry(lambda: yf.Ticker(sym).history(start="2011-01-01", end=end_day(), auto_adjust=False, actions=True))
        if h.empty:
            raise RuntimeError(f"yfinance returned nothing for {sym}")
        h.to_parquet(raw / f"{sym}.parquet")
        r = rebuild_raw(h)
        r.index = pd.to_datetime(r.index).strftime("%Y-%m-%d")
        r = r[r.index < lock.cutoff()]
        r["tr"] = r["split"] * (r["close"] + r["dividend"]) / r["close"].shift() - 1
        r.rename_axis("date").reset_index()[["date", "close", "tr"]].to_parquet(out / "bench" / f"{sym}.parquet")
    text = retry(lambda: requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS", timeout=60,
                                      headers={"User-Agent": "StockWatch research"}).text)
    (raw / "VIXCLS.csv").write_text(text)
    v = pd.read_csv(io.StringIO(text))
    v.columns = ["date", "vix"]
    v["vix"] = pd.to_numeric(v["vix"], errors="coerce")
    v[v["date"] < lock.cutoff()].dropna().to_parquet(out / "bench" / "vix.parquet")
    print("bench: SPY, RSP, VIX saved", flush=True)


def load(out):
    """Validated daily prices per CIK and everything the backtest reads, all before the cutoff."""
    m = out / "market"
    pm = pd.read_parquet(m / "price_map.parquet").dropna(subset=["cik"])
    pm["cik"] = pm["cik"].astype(int)
    t = ds.dataset(m / "prices", partitioning="hive").to_table(
        columns=["source", "symbol", "date", "open", "close", "dividend", "split"],
        filter=(pc.field("year") >= 2011) & (pc.field("date") < lock.cutoff())
        & pc.field("symbol").isin(sorted(set(pm["symbol"])))).to_pandas()
    lock.check(t["date"])
    p = t.merge(pm[["source", "symbol", "cik", "from", "to"]], on=["source", "symbol"])
    p = p[(p["date"] >= p["from"]) & (p["to"].isna() | (p["date"] <= p["to"]))]
    p = p.sort_values(["cik", "date", "from"]).drop_duplicates(["cik", "date"]).reset_index(drop=True)
    g = p.groupby("cik")
    prev, gap = g["close"].shift(), (pd.to_datetime(p["date"]) - pd.to_datetime(g["date"].shift())).dt.days
    sp, div = p["split"].fillna(1.0).replace(0, 1.0), p["dividend"].fillna(0.0)
    p["tr"] = (sp * (p["close"] + div) / prev - 1).where(gap <= 30)
    p["tro"] = (sp * (p["open"] + div) / prev - 1).where(gap <= 30)
    wide = {k: p.pivot(index="date", columns="cik", values=k).sort_index() for k in ["close", "tr", "tro"]}

    q = pd.read_parquet(m / "quality" / "issues.parquet")
    q = q[q["severity"] == "quarantine"].merge(pm[["source", "symbol", "cik", "from", "to"]], on=["source", "symbol"])
    q = q[(q["end"] >= q["from"]) & (q["to"].isna() | (q["start"] <= q["to"]))]
    blocked = {(r.cik, d) for r in q.itertuples() for d in pd.date_range(r.start, r.end).strftime("%Y-%m-%d")}

    last = p.groupby("cik")["date"].max()
    ended = last[last < sg.shift(p["date"].max(), -7)]
    k8 = pd.read_parquet(m / "filings_8k.parquet", columns=["cik", "filed", "items"])
    k8 = k8[k8["items"].fillna("").str.contains("1.03", regex=False) & k8["cik"].isin(ended.index)]
    failed = {c for c, L in ended.items() if ((k8["cik"] == c) & (k8["filed"] >= sg.shift(L, -365))
                                             & (k8["filed"] <= sg.shift(L, 30))).any()}

    co = pd.read_parquet(m / "companies.parquet").set_index("cik")
    facts = ds.dataset(m / "fundamentals" / "facts.parquet").to_table(
        columns=["cik", "tag", "start", "end", "value", "form", "filed", "accn"],
        filter=pc.field("tag").isin(sg.TAGS) & (pc.field("filed") < lock.cutoff())
        & pc.field("cik").isin(sorted(set(pm["cik"])))).to_pandas().sort_values(["end", "filed"])
    lock.check(facts["filed"])
    return dict(p=p, close=wide["close"], tr=wide["tr"], tro=wide["tro"], blocked=blocked, ended=ended,
                failed=failed, sector=co["sic"].map(gics11), facts=facts,
                segs=pd.read_parquet(m / "ticker_map.parquet"))


def rebalance_days(days, freq):
    s = pd.Series(days, index=pd.to_datetime(days))
    s = s[s.index >= START]
    key = s.index.to_period("W-FRI" if freq == "weekly" else "M")
    return list(s.groupby(key).last())


def build_signals(out, d):
    days = list(d["close"].index)
    rdays = sorted(set(rebalance_days(days, "weekly")) | set(rebalance_days(days, "monthly")))
    eq, splits, multi = sg.eps_quarters(d["facts"]), sg.splits_table(d["p"]), sg.multi_class(d["facts"])
    print(f"signals: {len(rdays)} days, {len(multi)} multi-class companies", flush=True)
    frames = []
    for i, day in enumerate(rdays):
        ciks = [c for c in members(d["segs"], day)["cik"] if c in d["close"].columns
                and pd.notna(d["close"].at[day, c])]
        r = sg.raw(day, ciks, d["close"], d["tr"], d["facts"], eq, splits, multi)
        frames.append(r.assign(date=day, sector=d["sector"].reindex(r.index).values).rename_axis("cik").reset_index())
        if i % 25 == 0:
            print(f"  {day}: {len(ciks)} priced members", flush=True)
    raw = pd.concat(frames, ignore_index=True)
    (out / "quant").mkdir(exist_ok=True)
    raw.to_parquet(out / "quant" / "raw.parquet", index=False)
    return raw


def scored(raw, choice, rule):
    """Scores for every day. rule "mean": the score is the mean rank; "rank": that mean re-ranked 0 to 100 among
    the day's scored stocks (docs/M3.md)."""
    out = []
    for day, r in raw.groupby("date"):
        r = r.set_index("cik")
        s = sg.score(r, r["sector"], choice)
        if rule == "rank":
            s["score"] = s["score"].rank(pct=True) * 100
        out.append(s.assign(date=day, sector=r["sector"], vol=r["vol"]))
    return pd.concat(out).rename_axis("cik").reset_index()


def forward(d, days):
    """Total return from each day's close to the next day's close in `days`."""
    tri = (1 + d["tr"].fillna(0)).cumprod().where(d["close"].notna())
    t = tri.loc[days]
    return (t.shift(-1) / t - 1).iloc[:-1]


def ic_table(raw, d):
    """Monthly rank correlation of every candidate (and the score) with the next month's total return."""
    months = rebalance_days(list(d["close"].index), "monthly")
    fwd = forward(d, months)
    base = scored(raw[raw["date"].isin(months)], BASE["choice"], "mean")
    cols = [c for v in sg.CANDIDATES.values() for c in v] + [s for s in sg.SIGNALS if s not in sg.CANDIDATES]
    rows = []
    for day, r in raw[raw["date"].isin(fwd.index)].groupby("date"):
        r = r.set_index("cik")
        f = fwd.loc[day].reindex(r.index)
        sec = r["sector"].fillna("Unknown")
        for c in cols:
            rk = r[c].rank(pct=True) if c in sg.PRICE else r[c].groupby(sec).rank(pct=True)
            rows.append({"date": day, "what": c, "ic": rk.corr(f, method="spearman"), "coverage": r[c].notna().mean()})
        s = base[base["date"] == day].set_index("cik")["score"]
        rows.append({"date": day, "what": "score", "ic": s.corr(f.reindex(s.index), method="spearman"),
                     "coverage": s.notna().mean()})
    t = pd.DataFrame(rows).groupby("what").agg(ic=("ic", "mean"), ic_sd=("ic", "std"), months=("ic", "count"),
                                                coverage=("coverage", "mean"))
    t["t_stat"] = t["ic"] / t["ic_sd"] * np.sqrt(t["months"])
    return t.round(4)


def eligible(raw):
    """How many stocks pass the buy rule's score part (score >= 80 and 5 of 7 ranks above 60) under each rule."""
    rows = []
    for rule in ["mean", "rank"]:
        s = scored(raw, BASE["choice"], rule)
        ok = (s["score"] >= 80) & (s["n60"] >= 5)
        g = s.assign(ok=ok).groupby("date").agg(scored=("score", "count"), eligible=("ok", "sum"))
        rows.append({"rule": rule, "scored_median": g["scored"].median(), "eligible_median": g["eligible"].median(),
                     "eligible_min": g["eligible"].min(), "eligible_max": g["eligible"].max(),
                     "days_under_10": int((g["eligible"] < 10).sum()), "days": len(g)})
    return pd.DataFrame(rows)


def metrics(nav, rf=0.0):
    r = nav.pct_change().dropna()
    years = len(r) / 252
    dd = (nav / nav.cummax() - 1).min()
    return {"cagr": (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1, "vol": r.std() * np.sqrt(252),
            "sharpe": (r.mean() - rf) / r.std() * np.sqrt(252), "max_drawdown": dd}


def simulate(d, sc, bench, vix, p):
    """Daily simulation. Holdings are units of each stock's total-return index, so splits and dividends need no
    bookkeeping. Returns (nav Series, stats dict)."""
    tr, tro, close = d["tr"], d["tro"], d["close"]
    days = [x for x in close.index if x >= START]
    tri = (1 + tr.fillna(0)).cumprod()
    tri_open = tri.shift() * (1 + tro)
    ma200, ma50 = tri.rolling(200, min_periods=150).mean(), tri.rolling(50, min_periods=40).mean()
    vol63 = tr.rolling(63, min_periods=50).std()
    spy = bench["SPY"].set_index("date")["close"]
    bad = (spy < spy.rolling(200).mean()) & (vix.set_index("date")["vix"].reindex(spy.index).ffill() > p["vix"])
    rset = set(rebalance_days(list(close.index), p["rebalance"]))
    by_day = {k: g.set_index("cik") for k, g in sc.groupby("date")}
    ended, failed = d["ended"], d["failed"]
    cash, units, strikes, peak = 1.0, {}, {}, {}
    orders, nav, traded, npos, cash_share, missed, exits = {}, {}, 0.0, [], [], 0, {"acquired": 0, "failed": 0}
    for t in days:
        if orders:  # at the open: sells first, then buys scaled to the cash there is
            for c, amt in sorted(orders.items(), key=lambda x: x[1]):
                px = tri_open.at[t, c] if c in tri_open.columns else np.nan
                if not px > 0:
                    missed += 1
                    continue
                if amt < 0:
                    u = min(units.get(c, 0.0), -amt / px)
                    units[c] = units.get(c, 0.0) - u
                    cash += u * px * (1 - p["cost"])
                    traded += u * px
                    if units[c] <= 1e-12:
                        units.pop(c), strikes.pop(c, None), peak.pop(c, None)
                else:
                    amt = min(amt, cash / (1 + p["cost"]))
                    if amt <= 0:
                        continue
                    units[c] = units.get(c, 0.0) + amt / px
                    cash -= amt * (1 + p["cost"])
                    traded += amt
            orders = {}
        for c in [c for c in units if ended.get(c) == t]:  # prices stop today: exit at the last close
            hit = FAILED_PENALTY if c in failed else 0.0
            cash += units.pop(c) * tri.at[t, c] * (1 - hit)
            exits["failed" if hit else "acquired"] += 1
            strikes.pop(c, None), peak.pop(c, None)
        val = {c: u * tri.at[t, c] for c, u in units.items() if pd.notna(tri.at[t, c])}
        total = cash + sum(val.values())
        nav[t] = total
        npos.append(len(units))
        cash_share.append(cash / total)
        if p["stop"]:  # challenger: exit on a fall from the peak since buying of 3 quarterly standard deviations
            for c, v in val.items():
                peak[c] = max(peak.get(c, 0.0), tri.at[t, c])
                s = vol63.at[t, c]
                if pd.notna(s) and tri.at[t, c] < peak[c] * (1 - 3 * s * np.sqrt(63)):
                    orders[c] = -np.inf
        if t not in rset:
            continue
        s = by_day.get(t)
        if s is None:
            continue
        mood_bad = p["filter"] != "none" and bool(bad.get(t, False))
        w = {c: v / total for c, v in val.items()}
        sells = {c for c in units if c not in s.index}  # left the index or unpriced
        if p["filter"] == "hard" and mood_bad:
            sells |= set(units)
        for c in units:
            sc_c = s["score"].get(c)
            if pd.notna(sc_c):
                strikes[c] = strikes.get(c, 0) + 1 if sc_c < p["sell"] else 0
                if strikes[c] >= p["strikes"]:
                    sells.add(c)
        keep = [c for c in units if c not in sells]
        cap = min(p["cap"], p["cap_x"] * p["avg"])
        for c in keep:
            if w.get(c, 0) > cap + 0.01:
                orders[c] = -(w[c] - cap) * total
        if not (p["filter"] == "hard" and mood_bad):
            bar = p["buy"] + (p["soft_raise"] if p["filter"] == "soft" and mood_bad else 0)
            cand = s[(s["score"] >= bar) & (s["n60"] >= p["n60"]) & ~s.index.isin(keep)]
            cand = cand.loc[[(c, t) not in d["blocked"] for c in cand.index]]
            if p["timing"]:
                ok = lambda c: tri.at[t, c] > ma200.at[t, c] and tri.at[t, c] < 1.25 * ma50.at[t, c] \
                    and tri.at[t, c] > 0.9 * tri[c].loc[:t].iloc[-22]
                cand = cand.loc[[bool(ok(c)) for c in cand.index]]
            cand = cand.sort_values("score", ascending=False)
            med_vol = s["vol"].median()
            held_sc = s["score"].reindex(keep).fillna(0).sort_values()
            sector_w = pd.Series({c: w.get(c, 0) for c in keep}).groupby(s["sector"].reindex(keep).fillna("Unknown")).sum() \
                if keep else pd.Series(dtype=float)
            room = p["max_pos"] - len(keep)
            for c, row in cand.iterrows():
                if room <= 0 and len(held_sc) and row["score"] >= held_sc.iloc[0] + p["swap"]:
                    out_c = held_sc.index[0]  # swap out the weakest holding
                    held_sc = held_sc.iloc[1:]
                    sells.add(out_c)
                    keep.remove(out_c)
                    sector_w = sector_w.sub(pd.Series({s["sector"].get(out_c, "Unknown") or "Unknown": w.get(out_c, 0)}),
                                            fill_value=0)
                    room += 1
                if room <= 0:
                    break
                size = p["avg"] * (med_vol / row["vol"] if pd.notna(row["vol"]) and row["vol"] > 0 else 1.0)
                if p["nudge"]:
                    size *= float(np.clip(1 + 0.2 * (row["score"] - 80) / 20, 0.8, 1.2))
                if p["filter"] == "soft" and mood_bad:
                    size *= p["soft_size"]
                size = float(np.clip(size, p["min_w"], cap))
                sec = row["sector"] if isinstance(row["sector"], str) else "Unknown"
                if sector_w.get(sec, 0) + size > p["sector_cap"]:
                    continue
                sector_w[sec] = sector_w.get(sec, 0) + size
                orders[c] = size * total
                room -= 1
        for c in sells:
            orders[c] = -np.inf
    nav = pd.Series(nav)
    years = len(nav) / 252
    stats = {"turnover": traded / nav.mean() / years / 2, "avg_positions": float(np.mean(npos)),
             "avg_cash": float(np.mean(cash_share)), "missed_orders": missed, **{f"exits_{k}": v for k, v in exits.items()}}
    return nav, stats


def bench_nav(bench, sym, days):
    r = bench[sym].set_index("date")["tr"].reindex(days).fillna(0)
    return (1 + r).cumprod()


def variations():
    """Name -> knobs changed from BASE. Every one is logged as an experiment."""
    v = {"base weekly": {}, "base monthly": {"rebalance": "monthly"}}
    for reb in ["weekly", "monthly"]:
        b = {"rebalance": reb}
        v |= {f"{reb}: no market filter": b | {"filter": "none"}, f"{reb}: hard market filter": b | {"filter": "hard"},
              f"{reb}: timing layer": b | {"timing": True}, f"{reb}: score nudge": b | {"nudge": True},
              f"{reb}: volatility stop": b | {"stop": True}, f"{reb}: score re-ranked": b | {"rule": "rank"}}
        for sig, cands in sg.CANDIDATES.items():
            for c in cands:
                if c != BASE["choice"][sig]:
                    v[f"{reb}: {sig} = {c}"] = b | {"choice": BASE["choice"] | {sig: c}}
        for k, vals in {"buy": [75.0, 85.0], "sell": [35.0, 45.0], "n60": [4, 6], "max_pos": [15, 25],
                        "avg": [1 / 12, 1 / 20], "swap": [15.0, 25.0], "cost": [0.002], "vix": [18.0, 25.0],
                        "sector_cap": [0.25, 0.35]}.items():
            for x in vals:
                v[f"{reb}: {k} = {round(x, 4)}"] = b | {k: x}
    return v


def run(out, d, raw):
    bench = {s: pd.read_parquet(out / "bench" / f"{s}.parquet") for s in ["SPY", "RSP"]}
    vix = pd.read_parquet(out / "bench" / "vix.parquet")
    lock.check(list(bench["SPY"]["date"]) + list(vix["date"]))
    cache, rows = {}, []
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    exp = out / "inbox" / "experiments"
    exp.mkdir(parents=True, exist_ok=True)
    for name, knobs in variations().items():
        p = BASE | knobs
        key = (json.dumps(p["choice"], sort_keys=True), p["rule"])
        if key not in cache:
            cache[key] = scored(raw, p["choice"], p["rule"])
        nav, stats = simulate(d, cache[key], bench, vix, p)
        m = metrics(nav)
        res = {**{k: round(float(x), 4) for k, x in m.items()}, **{k: round(float(x), 4) for k, x in stats.items()}}
        for sym in ["SPY", "RSP"]:
            bm = metrics(bench_nav(bench, sym, nav.index))
            res |= {f"{sym.lower()}_{k}": round(float(x), 4) for k, x in bm.items()}
        ex = nav.pct_change().dropna() - bench_nav(bench, "SPY", nav.index).pct_change().dropna()
        res["excess_t_stat"] = round(float(ex.mean() / ex.std() * np.sqrt(len(ex))), 2)
        res["wins"] = bool(res["cagr"] > res["spy_cagr"] and res["sharpe"] >= res["spy_sharpe"]
                           and res["max_drawdown"] >= 1.5 * res["spy_max_drawdown"])
        rows.append({"name": name, **res})
        print(f"{name}: {res}", flush=True)
        params = {k: v for k, v in p.items()} | {"period": f"{START} to {nav.index[-1]}"}
        rec = {"created_at": stamp, "name": f"M3 tuning: {name}", "params": json.dumps(params, sort_keys=True),
               "result": json.dumps(res, sort_keys=True), "note": "2013 to 2019 tuning years, test years locked"}
        safe = "".join(ch if ch.isalnum() else "-" for ch in name)
        (exp / f"{stamp[:10]}-{safe}.json").write_text(json.dumps(rec))
    pd.DataFrame(rows).to_csv(out / "review" / "m3_backtest.csv", index=False)


def main(out_dir, step):
    out = Path(out_dir)
    (out / "review").mkdir(parents=True, exist_ok=True)
    if step == "bench":
        return bench(out)
    d = load(out)
    print(f"loaded {d['close'].shape[1]} companies, {len(d['close'])} days, {len(d['ended'])} series end early, "
          f"{len(d['failed'])} failed, {len(d['blocked'])} quarantined company-days", flush=True)
    rawp = out / "quant" / "raw.parquet"
    if step == "signals" or not rawp.exists():
        raw = build_signals(out, d)
        t = ic_table(raw, d)
        t.to_csv(out / "review" / "m3_ic.csv")
        e = eligible(raw)
        e.to_csv(out / "review" / "m3_eligible.csv", index=False)
        print(t.to_string(), "\n", e.to_string(), flush=True)
    else:
        raw = pd.read_parquet(rawp)
    if step == "run":
        run(out, d, raw)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
