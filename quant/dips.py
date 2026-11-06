"""Leader-dip study on the 2013 to 2019 tuning years only (the lock enforces it). PLAN.md, changes 2026-11-05.

The question: when one of the biggest companies in a strong industry falls about 20 to 25% from its high, is it a good
buy again? For every such dip, its total return over the next 3, 6 and 12 months against SPY's, how often it beat SPY,
and how often it fell another 10% first. The same dips in every S&P 500 stock, and leaders on any Friday, are the
comparisons.

  leader     each month-end, the 100 largest S&P 500 members by market value (as known that day)
  strong     the stock's SIC 2-digit industry has an equal-weight 12-month return in the top third of industries that
             month (industries with at least 3 members)
  tech       SIC 357x, 367x, 737x: computers, semiconductors and electronics, software. The only part of a theme list
             like "AI and semiconductors" that 2013 to 2019 can test (no SIC code says AI)
  dip X      the first Friday close X% or more below the highest close of the last 252 trading days (total-return
             index); it re-arms once the stock is back within X/2 of its high
  forward    total return from that Friday's close over 63, 126 and 252 trading days, minus SPY's; a company that
             stops trading keeps its last value, less FAILED_PENALTY if it failed. Windows must end before 2020.

Events cluster (many fall in the same weeks), so the t-statistic is on monthly averages, not single events.

Usage: python -m quant.dips OUT_DIR  ->  review/dips_events.csv, review/dips_summary.csv, inbox/experiments/*.json
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from quant import backtest as bt, lock, signals as sg

LEADERS, TOP_THIRD, MIN_INDUSTRY = 100, 2 / 3, 3
DIPS = [0.15, 0.20, 0.25, 0.30]
HORIZONS = {"3m": 63, "6m": 126, "12m": 252}
TECH = ("357", "367", "737")


def tri_of(tr, close):
    return (1 + tr.fillna(0)).cumprod().where(close.notna())


def monthly_flags(d, tri, sic):
    """month-end -> DataFrame(cik: leader, strong, tech) for that month's members."""
    eq, splits, multi = sg.eps_quarters(d["facts"]), sg.splits_table(d["p"]), sg.multi_class(d["facts"])
    days = list(d["close"].index)
    out = {}
    for m in bt.rebalance_days(days, "monthly", "2013-01-01"):
        ciks = [c for c in bt.members(d["segs"], m)["cik"] if c in d["close"].columns and pd.notna(d["close"].at[m, c])]
        mv = sg.raw(m, ciks, d["close"], d["tr"], d["facts"], eq, splits, multi)["mv"]
        i = days.index(m)
        r12 = tri.iloc[i][ciks] / tri.iloc[max(i - 252, 0)][ciks] - 1
        f = pd.DataFrame({"mv": mv, "r12": r12, "sic2": sic.reindex(ciks).str[:2].values,
                          "tech": sic.reindex(ciks).str[:3].isin(TECH).values}, index=ciks)
        ind = f.groupby("sic2")["r12"].agg(["mean", "count"])
        ind = ind[ind["count"] >= MIN_INDUSTRY]["mean"]
        strong = set(ind[ind.rank(pct=True) > TOP_THIRD].index)
        f["leader"] = f["mv"].rank(ascending=False) <= LEADERS
        f["strong"] = f["sic2"].isin(strong)
        out[m] = f[["leader", "strong", "tech"]]
        print(f"  {m}: {len(ciks)} members, {int(f['leader'].sum())} leaders, {len(strong)} strong industries",
              flush=True)
    return out


def events(d, tri, flags):
    days = list(d["close"].index)
    hi = tri.rolling(252, min_periods=200).max()
    dd = tri / hi - 1
    months = sorted(flags)
    armed = {x: {} for x in DIPS}
    rows, base = [], []
    for t in bt.rebalance_days(days, "weekly", "2013-02-01"):
        m = max((x for x in months if x <= t), default=None)
        if m is None:
            continue
        f = flags[m]
        members = [c for c in bt.members(d["segs"], t)["cik"] if c in dd.columns and c in f.index]
        for c in members:
            v = dd.at[t, c]
            if pd.isna(v) or (c, t) in d["blocked"]:
                continue
            if f.at[c, "leader"]:
                base.append({"date": t, "cik": c, "strong": bool(f.at[c, "strong"]), "tech": bool(f.at[c, "tech"])})
            for x in DIPS:
                if armed[x].get(c, True) and v <= -x:
                    rows.append({"date": t, "cik": c, "dip": x, "drawdown": round(float(v), 4),
                                 **{k: bool(f.at[c, k]) for k in ("leader", "strong", "tech")}})
                    armed[x][c] = False
                elif v > -x / 2:
                    armed[x][c] = True
    return pd.DataFrame(rows), pd.DataFrame(base)


def forward(d, tri, spy, ev):
    days = list(d["close"].index)
    pos = {t: i for i, t in enumerate(days)}
    out = ev.copy()
    for name, h in HORIZONS.items():
        ex, fell = [], []
        for r in ev.itertuples():
            i = pos[r.date]
            if i + h >= len(days):  # the window would reach 2020: locked
                ex.append(np.nan)
                fell.append(np.nan)
                continue
            path = tri[r.cik].iloc[i:i + h + 1]
            last = path.dropna()
            ret = last.iloc[-1] / last.iloc[0] - 1
            if last.index[-1] != days[i + h] and r.cik in d["failed"]:
                ret = (1 + ret) * (1 - bt.FAILED_PENALTY) - 1
            ex.append(ret - (spy.iloc[i + h] / spy.iloc[i] - 1))
            fell.append(float((last / last.iloc[0] - 1).min() <= -0.10))
        out[f"excess_{name}"], out[f"fell_10_{name}"] = ex, fell
    return out


def summary(ev, base):
    groups = {"leaders, strong industry": ev["leader"] & ev["strong"], "leaders, tech": ev["leader"] & ev["tech"],
              "leaders, any industry": ev["leader"], "every S&P 500 stock": pd.Series(True, ev.index)}
    rows = []
    for x in DIPS:
        for g, mask in groups.items():
            e = ev[mask & (ev["dip"] == x)]
            for name in HORIZONS:
                v = e.dropna(subset=[f"excess_{name}"])
                mon = v.groupby(v["date"].str[:7])[f"excess_{name}"].mean()
                rows.append({"dip": x, "group": g, "horizon": name, "events": len(v),
                             "companies": v["cik"].nunique(), "months": len(mon),
                             "mean_excess": v[f"excess_{name}"].mean(), "median_excess": v[f"excess_{name}"].median(),
                             "beat_spy": (v[f"excess_{name}"] > 0).mean(), "fell_10_more": v[f"fell_10_{name}"].mean(),
                             "t_monthly": mon.mean() / mon.std() * np.sqrt(len(mon)) if len(mon) > 2 else np.nan})
    for g, mask in {"leaders, strong industry": base["strong"], "leaders, tech": base["tech"],
                    "leaders, any industry": pd.Series(True, base.index)}.items():
        b = base[mask]
        for name in HORIZONS:
            v = b.dropna(subset=[f"excess_{name}"])
            rows.append({"dip": 0.0, "group": f"{g}, any Friday (no dip)", "horizon": name, "events": len(v),
                         "companies": v["cik"].nunique(), "months": v["date"].str[:7].nunique(),
                         "mean_excess": v[f"excess_{name}"].mean(), "median_excess": v[f"excess_{name}"].median(),
                         "beat_spy": (v[f"excess_{name}"] > 0).mean(), "fell_10_more": v[f"fell_10_{name}"].mean(),
                         "t_monthly": np.nan})
    return pd.DataFrame(rows).round(4)


def main(out_dir):
    out = Path(out_dir)
    (out / "review").mkdir(parents=True, exist_ok=True)
    d = bt.load(out)
    spy = pd.read_parquet(out / "bench" / "SPY.parquet").set_index("date")
    lock.check(spy.index)
    tri = tri_of(d["tr"], d["close"])
    spy = (1 + spy["tr"].reindex(tri.index).fillna(0)).cumprod()
    sic = pd.read_parquet(out / "market" / "companies.parquet").set_index("cik")["sic"].astype("string")
    flags = monthly_flags(d, tri, sic)
    ev, base = events(d, tri, flags)
    print(f"{len(ev)} dip events, {len(base)} leader-Fridays", flush=True)
    ev, base = forward(d, tri, spy, ev), forward(d, tri, spy, base)
    ev.to_csv(out / "review" / "dips_events.csv", index=False)
    s = summary(ev, base)
    s.to_csv(out / "review" / "dips_summary.csv", index=False)
    pd.set_option("display.width", 250)
    print(s.to_string(index=False), flush=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    exp = out / "inbox" / "experiments"
    exp.mkdir(parents=True, exist_ok=True)
    params = {"leaders": LEADERS, "strong": "SIC 2-digit industry, 12-month equal-weight return, top third",
              "tech": list(TECH), "dips": DIPS, "rearm": "back within half the dip", "horizons": HORIZONS,
              "period": f"{min(ev['date'])} to {max(ev['date'])}, windows end before 2020"}
    rec = {"created_at": stamp, "name": "Leader-dip study (PLAN changes 2026-11-05)",
           "params": json.dumps(params, sort_keys=True), "result": s.to_json(orient="records"),
           "note": "2013 to 2019 tuning years, test years locked; an event study, not a backtest of a strategy"}
    (exp / f"{stamp[:19].replace(':', '')}-leader-dips.json").write_text(json.dumps(rec))


if __name__ == "__main__":
    main(sys.argv[1])
