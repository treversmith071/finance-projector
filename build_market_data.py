#!/usr/bin/env python3
"""
Build the vendored historical returns table used by the Retirement calculator.

Source: Robert Shiller's long-run dataset (monthly S&P 500 price, annualized
dividend, CPI, and the long-term interest rate / GS10), 1871-present. We use the
CSV mirror at:
    https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv

For each calendar year Y (Jan Y -> Jan Y+1) we compute nominal annual:
  * stock_total_return = (P_end - P_start + D) / P_start        (S&P 500 w/ divs)
  * inflation          = CPI_end / CPI_start - 1                (CPI)
  * bond_total_return  = 10-year par-bond one-year holding return, i.e. buy a par
                         bond at last year's long rate y0, hold one year (now a
                         9-year bond), reprice at this year's long rate y1:
                             price = y0 * (1 - (1+y1)^-9)/y1 + (1+y1)^-9
                             return = y0 + price - 1
    (the standard rolling-constant-maturity approximation used by FIRECalc-style
     simulators to turn the yield series into a total-return series)

Output: market_data.json  -> {"source","method","years":[y0..],"data":[[year,
stock,bond,inflation], ...]}

Usage:
    curl -s https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv -o shiller.csv
    python3 build_market_data.py shiller.csv
"""
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "market_data.json")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def bond_year_return(y0: float, y1: float) -> float:
    """One-year total return of a 10-year par bond bought at yield y0, repriced a
    year later (9 years left) at yield y1. Yields are decimals (e.g. 0.05)."""
    coupon = y0
    if y1 <= 0:
        price = 1.0 + coupon * 9  # degenerate; shouldn't occur in this data
    else:
        price = coupon * (1 - (1 + y1) ** -9) / y1 + (1 + y1) ** -9
    return coupon + price - 1.0


def build(csv_path: str) -> dict:
    rows = list(csv.reader(open(csv_path)))[1:]  # drop header
    # Map year -> January row (Date, SP500, Dividend, Earnings, CPI, LongRate, ...)
    jan = {}
    for r in rows:
        if len(r) < 6 or not r[0]:
            continue
        ym = r[0]
        if ym[5:7] != "01":
            continue
        year = int(ym[:4])
        price, div, cpi, rate = _f(r[1]), _f(r[2]), _f(r[4]), _f(r[5])
        if price > 0 and cpi > 0 and rate > 0:
            jan[year] = {"price": price, "div": div, "cpi": cpi, "rate": rate / 100.0}

    data = []
    for year in sorted(jan):
        a, b = jan.get(year), jan.get(year + 1)
        if not b:
            continue
        stock = (b["price"] - a["price"] + a["div"]) / a["price"]
        infl = b["cpi"] / a["cpi"] - 1.0
        bond = bond_year_return(a["rate"], b["rate"])
        data.append([year, round(stock, 6), round(bond, 6), round(infl, 6)])

    # Recent-year supplement: the Shiller CSV mirror is stale (~2023), so append
    # the latest full calendar years from public sources. These 3 rows use
    # calendar-year total returns (vs Jan-to-Jan for the Shiller-derived series);
    # the tiny convention difference is immaterial. Sources: S&P 500 total return
    # (slickcharts/macrotrends): 2023 +26.15%, 2024 +24.82%, 2025 +17.68%. CPI
    # (BLS): 2023 +3.35%, 2024 +2.9%, 2025 +2.7%. 10yr Treasury total return
    # (approx): 2023 +3.5% (flat yields), 2024 -1.7% (rising yields), 2025 +3.0%.
    RECENT = {
        2023: (0.2615, 0.035, 0.0335),
        2024: (0.2482, -0.017, 0.029),
        2025: (0.1768, 0.030, 0.027),
    }
    have = {d[0] for d in data}
    for y in sorted(RECENT):
        if y not in have:
            s, b, i = RECENT[y]
            data.append([y, round(s, 6), round(b, 6), round(i, 6)])

    return {
        "source": "Shiller long-run dataset (S&P 500, CPI, long rate), 1871+ via "
                  "github.com/datasets/s-and-p-500, extended with 2023-2025 public "
                  "calendar-year figures (S&P TR, CPI, ~10yr Treasury TR).",
        "method": "Jan-to-Jan nominal annual: stock=price change + dividends; "
                  "bond=10yr par-bond 1yr roll return from the long rate; "
                  "inflation=CPI change. 2023-2025 rows are calendar-year totals.",
        "years": [d[0] for d in data],
        "columns": ["year", "stock_total_return", "bond_total_return", "inflation"],
        "data": data,
    }


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: python3 build_market_data.py <shiller.csv>", file=sys.stderr)
        return 1
    out = build(argv[1])
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    d = out["data"]
    print(f"Wrote {OUT}: {len(d)} years ({d[0][0]}-{d[-1][0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
