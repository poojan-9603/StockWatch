"""Point-in-time XBRL facts from SEC companyfacts.zip.

Every filing's copy of a number is kept with its filed date and accession, so a calculation can use only what was
public on a given day, and later restatements don't leak backward (fundamentals count from the filing date).

Scope: the companies in ticker_map (S&P 500 members since 2010) plus companies on NYSE or Nasdaq today (the live
universe is drawn from these). Tags: TAGS below. The raw zip in R2 keeps every tag, so adding one is a re-run,
not a re-download.

Also writes review/tag_coverage.csv: for companies in the index at some point in the 2013 to 2019 tuning years,
how many filed each tag in a 10-K during those years. It shows which tags the quant engine can rely on.

Usage: python -m pipeline.fundamentals OUT_DIR  (after pipeline.sec and pipeline.ticker_map)
  writes OUT_DIR/raw/sec/companyfacts.zip, OUT_DIR/market/fundamentals/facts.parquet, OUT_DIR/review/tag_coverage.csv
"""
import json
import re
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from pipeline.sec import download

COMPANYFACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
TUNING = ("2013-01-01", "2020-01-01")
TAGS = {
    "dei": ["EntityCommonStockSharesOutstanding", "EntityPublicFloat"],
    "us-gaap": [
        # balance sheet
        "Assets", "AssetsCurrent", "Liabilities", "LiabilitiesCurrent", "LiabilitiesAndStockholdersEquity",
        "StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "MinorityInterest", "CashAndCashEquivalentsAtCarryingValue", "AccountsReceivableNetCurrent", "InventoryNet",
        "PropertyPlantAndEquipmentNet", "Goodwill", "IntangibleAssetsNetExcludingGoodwill",
        "LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtCurrent", "DebtCurrent",
        # income statement
        "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet", "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "GrossProfit",
        "SellingGeneralAndAdministrativeExpense", "ResearchAndDevelopmentExpense", "OperatingIncomeLoss",
        "InterestExpense", "IncomeTaxExpenseBenefit",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "EarningsPerShareBasic",
        "EarningsPerShareDiluted", "DepreciationDepletionAndAmortization",
        # cash flow
        "NetCashProvidedByUsedInOperatingActivities", "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsOfDividends", "PaymentsOfDividendsCommonStock", "PaymentsForRepurchaseOfCommonStock",
        "ProceedsFromIssuanceOfCommonStock",
        # shares and dividends
        "CommonStockSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfDilutedSharesOutstanding", "CommonStockDividendsPerShareDeclared",
    ],
}
SCHEMA = pa.schema([("cik", pa.int64()), ("taxonomy", pa.string()), ("tag", pa.string()), ("unit", pa.string()),
                    ("start", pa.string()), ("end", pa.string()), ("value", pa.float64()), ("fy", pa.int32()),
                    ("fp", pa.string()), ("form", pa.string()), ("filed", pa.string()), ("accn", pa.string()),
                    ("frame", pa.string())])


def extract(zip_path, scope, tuning_ciks, dest):
    """Write whitelisted facts for `scope` CIKs to `dest`. Returns (companies read, facts written,
    {(taxonomy, tag): tuning companies with a 10-K value filed 2013 to 2019}, tuning companies with no facts)."""
    want = {(tx, tag) for tx, tags in TAGS.items() for tag in tags}
    cover, seen = Counter(), set()
    writer, batch, n_rows = pq.ParquetWriter(dest, SCHEMA), [], 0

    def flush():
        nonlocal batch, n_rows
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, SCHEMA))
            n_rows += len(batch)
            batch = []

    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            m = re.fullmatch(r"CIK(\d{10})\.json", name)
            if not m or int(m.group(1)) not in scope:
                continue
            cik = int(m.group(1))
            try:
                d = json.loads(z.read(name))
            except json.JSONDecodeError as e:
                raise ValueError(f"unreadable company file {name} in companyfacts.zip: {e}") from e
            seen.add(cik)
            for tx, tags in d.get("facts", {}).items():
                for tag, body in tags.items():
                    keep, in_10k = (tx, tag) in want, False
                    for unit, obs in body["units"].items():
                        for o in obs:
                            in_10k = in_10k or (o.get("form") == "10-K" and TUNING[0] <= o["filed"] < TUNING[1])
                            if keep:
                                batch.append({"cik": cik, "taxonomy": tx, "tag": tag, "unit": unit,
                                              "start": o.get("start"), "end": o["end"], "value": float(o["val"]),
                                              "fy": o.get("fy"), "fp": o.get("fp"), "form": o.get("form"),
                                              "filed": o["filed"], "accn": o["accn"], "frame": o.get("frame")})
                    if in_10k and cik in tuning_ciks:
                        cover[(tx, tag)] += 1
            if len(batch) > 500_000:
                flush()
    flush()
    writer.close()
    return len(seen), n_rows, cover, tuning_ciks - seen


def main(out_dir):
    out = Path(out_dir)
    zip_path = out / "raw" / "sec" / "companyfacts.zip"
    print("companyfacts.zip Last-Modified:", download(COMPANYFACTS_URL, zip_path))  # raw first, latest copy only

    segs = pd.read_parquet(out / "market" / "ticker_map.parquet").dropna(subset=["cik"])
    companies = pd.read_parquet(out / "market" / "companies.parquet")
    listed = companies[companies["exchanges"].str.contains(r"\b(?:NYSE|Nasdaq)\b", regex=True)]
    scope = set(segs["cik"].astype(int)) | set(listed["cik"])
    in_tuning = segs[(segs["start"] < TUNING[1]) & (segs["end"].isna() | (segs["end"] > TUNING[0]))]
    tuning_ciks = set(in_tuning["cik"].astype(int))

    (out / "market" / "fundamentals").mkdir(parents=True, exist_ok=True)
    t = time.monotonic()
    n_co, n_rows, cover, no_facts = extract(zip_path, scope, tuning_ciks, out / "market" / "fundamentals" / "facts.parquet")
    print(f"facts: {n_rows} rows for {n_co} of {len(scope)} companies in scope, {time.monotonic() - t:.0f}s")

    names = companies.set_index("cik")["name"]
    wl = {(tx, tag) for tx, tags in TAGS.items() for tag in tags}
    cov = pd.DataFrame([{"taxonomy": tx, "tag": tag, "companies": n, "share": round(n / len(tuning_ciks), 3),
                         "whitelisted": (tx, tag) in wl} for (tx, tag), n in cover.items()])
    cov = cov[(cov["share"] >= 0.05) | cov["whitelisted"]].sort_values("companies", ascending=False)
    (out / "review").mkdir(parents=True, exist_ok=True)
    cov.to_csv(out / "review" / "tag_coverage.csv", index=False)
    print(f"tuning-years companies: {len(tuning_ciks)}; with no XBRL facts at all: {len(no_facts)}")
    print("  e.g.", sorted(names.get(k, k) for k in no_facts)[:15])
    print(cov[cov["whitelisted"]].head(60).to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1])
