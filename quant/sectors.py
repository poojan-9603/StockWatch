"""SIC code -> sector. Three candidates, measured in M3 (docs/M3.md):

  division   the SIC division letter (A to J), SIC's own top level
  office     the SEC review office that handles the company (companies.owner_org)
  gics11     SIC major groups and a few sub-groups mapped to the 11 GICS-style sectors: an approximation of the sector
             lists investors use, so sector-relative ranks compare like with like
"""

DIVISIONS = [(1, 9, "A Agriculture"), (10, 14, "B Mining"), (15, 17, "C Construction"), (20, 39, "D Manufacturing"),
             (40, 49, "E Transport, communications, utilities"), (50, 51, "F Wholesale"), (52, 59, "G Retail"),
             (60, 67, "H Finance"), (70, 89, "I Services"), (91, 99, "J Public administration")]

# (low SIC, high SIC, sector), most specific first: the first range containing the code wins
GICS11 = [
    (2830, 2836, "Health Care"), (2840, 2844, "Consumer Staples"), (3570, 3579, "Information Technology"),
    (3630, 3639, "Consumer Discretionary"), (3660, 3679, "Information Technology"), (3710, 3716, "Consumer Discretionary"),
    (3820, 3829, "Health Care"), (3840, 3851, "Health Care"), (4950, 4959, "Industrials"), (5120, 5122, "Health Care"),
    (5140, 5149, "Consumer Staples"), (5311, 5399, "Consumer Staples"), (5400, 5499, "Consumer Staples"),
    (5912, 5912, "Consumer Staples"), (6798, 6798, "Real Estate"), (7370, 7370, "Communication Services"),
    (7371, 7379, "Information Technology"), (7800, 7899, "Communication Services"), (8731, 8731, "Health Care"),
    (100, 999, "Consumer Staples"), (1000, 1099, "Materials"), (1200, 1399, "Energy"), (1400, 1499, "Materials"),
    (1500, 1799, "Industrials"), (2000, 2199, "Consumer Staples"), (2200, 2399, "Consumer Discretionary"),
    (2400, 2499, "Materials"), (2500, 2599, "Consumer Discretionary"), (2600, 2699, "Materials"),
    (2700, 2799, "Communication Services"), (2800, 2899, "Materials"), (2900, 2999, "Energy"),
    (3000, 3099, "Materials"), (3100, 3199, "Consumer Discretionary"), (3200, 3399, "Materials"),
    (3400, 3499, "Industrials"), (3500, 3599, "Industrials"), (3600, 3699, "Industrials"), (3700, 3799, "Industrials"),
    (3800, 3899, "Information Technology"), (3900, 3999, "Consumer Discretionary"), (4000, 4799, "Industrials"),
    (4800, 4899, "Communication Services"), (4900, 4999, "Utilities"), (5000, 5199, "Industrials"),
    (5200, 5999, "Consumer Discretionary"), (6000, 6499, "Financials"), (6500, 6599, "Real Estate"),
    (6700, 6799, "Financials"), (7000, 7099, "Consumer Discretionary"), (7200, 7299, "Consumer Discretionary"),
    (7300, 7399, "Industrials"), (7500, 7599, "Consumer Discretionary"), (7900, 7999, "Consumer Discretionary"),
    (8000, 8099, "Health Care"), (8200, 8299, "Consumer Discretionary"), (8700, 8799, "Industrials"),
    (9900, 9999, "Industrials"),
]


def division(sic):
    try:
        two = int(str(sic)[:2]) if len(str(sic)) == 4 else int(sic) // 100
    except (TypeError, ValueError):
        return None
    return next((name for lo, hi, name in DIVISIONS if lo <= two <= hi), None)


def gics11(sic):
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return None
    return next((name for lo, hi, name in GICS11 if lo <= s <= hi), None)
