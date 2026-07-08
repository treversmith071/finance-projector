#!/usr/bin/env python3
"""
Finance projector for a hub-account CSV.

Personal baseline figures (rent, biweekly deposit, 401k contribution, whether
you receive bonuses and the bonus threshold) are read from a machine-local,
git-ignored finance_config.json (never committed). On a fresh clone the values
are collected through the dashboard's first-run onboarding UI (not the terminal)
and saved back via the local bridge; see finance_config.example.json for the
schema.

Usage:
    python3 project.py <transactions.csv> [--as-of YYYY-MM-DD]
                                          [--k401-annual N]
                                          [--biweekly-deposit N]
                                          [--rent N]
                                          [--net-venmo / --gross-venmo]
                                          [--net-gambling / --gross-gambling]
                                          [--td-as-savings / --td-as-spending]

CSV format (header row required):
    Date, Time, Amount, Type, Description

Conventions baked in (from prior reconciliation):
  * Hub account = the account this CSV is exported from.
  * Anything that accumulates in the hub account counts as SAVINGS.
  * Withdrawals to Robinhood / Wealthfront / configured savings accounts = SAVINGS.
  * Withdrawals to the configured spending account = SPENDING (it pays expenses).
  * Plus the configured biweekly deposit direct-deposited into that account from
    elsewhere (not in this CSV); assumed fully spent.
  * Venmo netted by default (cash-ins and OUT cancel; net inflow stays in
    main accumulation as savings).
  * Gambling netted by default (small impact either way).
  * TD checking transfers treated as savings vehicle (one-time per year).
  * Large ADP payroll deposit (>$15k) flagged as bonus → one-time.
  * 401k assumed evenly spread across 26 biweekly paychecks.
"""

import argparse
import calendar
import csv
import glob
import json
import math
import os
import re
import sys

import app_paths
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


# ──────────────────────────────────────────────────────────────────────────────
# Classification
# ──────────────────────────────────────────────────────────────────────────────
def classify(desc: str, amt: float) -> str:
    # Normalize whitespace so "IRS  TREAS" (double space) matches "irs treas"
    d = re.sub(r"\s+", " ", desc.strip().lower())

    # Rent — pre-Apr 2025 was paid as HRV; bucket it explicitly so we can
    # subtract it when comparing periods with different rent amounts.
    if "hrvmanagement" in d:                          return "RENT"

    if "wealthfront" in d:                            return "INVEST_WEALTHFRONT"
    if "robinhood" in d:                              return "INVEST_ROBINHOOD"
    # Transfers between the user's own accounts are identified by the words in
    # the description ("… transfer … Savings account …", "… transfer … Spending
    # account …") — no account numbers needed. Requiring "transfer" avoids
    # catching peer payments (e.g. a Zelle deposit that names an account). The
    # mapping screen lets the user re-bucket these.
    if "transfer" in d and "savings account" in d:    return "SAVINGS_XFER"
    if "transfer" in d and "spending account" in d:   return "SPEND_MAIN"

    # Payroll: ADP TOTALSOURCE PAYROLL/DIRECT DEP (2026 + late 2025)
    # and ASF, DBA Insperi PAYROLL (early/mid 2025).
    if "adp totalsource" in d \
       or "asf, dba insperi" in d \
       or "asf dba insperi" in d:                     return "PAYROLL"

    if "nysttaxrfd" in d or "irs treas" in d \
       or d == "irs" or d.startswith("irs "):         return "TAX_REFUND"
    if "interest paid" in d:                          return "INTEREST"
    if "atm fee reimbursement" in d:                  return "ATM_REIMB"
    if "splitwise" in d:                              return "SPLITWISE"

    # Cover Whale insurance ACH — recurring side income.
    if "cover whale" in d:                            return "OTHER_IN"

    if "venmo cashout" in d or "venmo refund" in d:   return "VENMO_IN"
    if "venmo payment" in d or "venmo purchase" in d: return "VENMO_OUT"

    # Zelle — peer-payment, treated like Venmo. Direction defaults from the
    # amount sign; the mapping screen can re-bucket it.
    if "zelle payment" in d:
        return "VENMO_IN" if amt > 0 else "VENMO_OUT"

    # Gambling: sportsbooks, casinos, prediction markets, racing.
    if any(k in d for k in ("fd sptsbk", "fanduel", "draftkings",
                            "kalshi", "klear kalshi", "tioga")):
        return "GAMBLING_IN" if amt > 0 else "GAMBLING_OUT"

    # Direct retail / ATM / merchant.
    if any(k in d for k in ("spectrum", "tnsmt", "you garden",
                            "mcloughlins", "tobacco convenience",
                            "citizens bank", "chase astoria",
                            "mulcahys", "duane rea", "pai iso",
                            "qt 711", "mgm grand", "evi*")):
        return "DIRECT_EXP"
    if d.startswith("check paid"):                    return "DIRECT_EXP"

    if "echeck deposit" in d:                         return "OTHER_IN"
    if "requested transfer from" in d \
       and "ally bank" in d:                          return "OTHER_IN"

    # Anything else defaults by amount sign (inflow → income, outflow → spending)
    # and can be re-bucketed in the mapping screen.
    return "OTHER_IN" if amt > 0 else "UNCLASSIFIED"


# ──────────────────────────────────────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Row:
    date: date
    amount: float
    desc: str
    cat: str


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%-m/%-d/%y", "%-m/%-d/%Y")


def _parse_date(s: str) -> date | None:
    s = s.strip().strip('"')
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _split_line(line: str) -> list[str] | None:
    """Split one transaction line into up to 5 fields, regardless of whether
    the input is comma-, tab-, or multi-space-delimited (Excel paste)."""
    line = line.rstrip("\n").rstrip("\r")
    if not line.strip():
        return None

    # 1. Comma-separated (handles quoted fields with embedded commas).
    if "," in line and "\t" not in line:
        try:
            row = next(csv.reader([line]))
            if len(row) >= 5 and _parse_date(row[0]) is not None:
                # Last field absorbs any trailing comma-bearing description chunks.
                return [row[0], row[1], row[2], row[3], ",".join(row[4:])]
        except Exception:
            pass

    # 2. Tab-separated (most Excel pastes).
    if "\t" in line:
        parts = line.split("\t")
        parts = [p.strip() for p in parts if p.strip() != ""]
        if len(parts) >= 5 and _parse_date(parts[0]) is not None:
            return [parts[0], parts[1], parts[2], parts[3],
                    "\t".join(parts[4:])]

    # 3. Multi-space-separated (other Excel/UI pastes).
    parts = re.split(r" {2,}", line.strip(), maxsplit=4)
    if len(parts) >= 5 and _parse_date(parts[0]) is not None:
        return parts

    return None


def load(path: str, apply_buckets: bool = True) -> list[Row]:
    """Parse transactions from CSV, TSV, or Excel-pasted text. When
    apply_buckets is False the raw classify() category is kept (used to build the
    mapping screen's groups); otherwise the user's group→bucket override is
    applied."""
    out = []
    with open(path, newline="") as f:
        lines = f.readlines()

    for raw in lines:
        fields = _split_line(raw)
        if fields is None:
            continue
        d = _parse_date(fields[0])
        if d is None:
            continue  # likely the header row
        try:
            amt = float(fields[2].strip().strip('"'))
        except ValueError:
            continue
        desc = fields[4].strip().strip('"')
        cat = classify(desc, amt)
        if apply_buckets:
            cat = apply_group_bucket(cat, group_key(desc))
        out.append(Row(d, amt, desc, cat))

    return out


def build_groups(path: str) -> list[dict]:
    """Group parsed transactions by payee/destination for the mapping screen.
    Each group carries a display label, row count, net + gross totals, and a
    smart-default bucket (the natural bucket of its dominant category). The
    user's saved override, if any, is surfaced as `bucket`."""
    rows = load(path, apply_buckets=False)
    agg: dict = {}
    for r in rows:
        key = group_key(r.desc)
        g = agg.get(key)
        if g is None:
            g = agg[key] = {"key": key, "label": group_label(r.desc), "count": 0,
                            "net": 0.0, "gross": 0.0, "_weight": defaultdict(float),
                            "_catw": defaultdict(float), "sample": r.desc}
        g["count"] += 1
        g["net"] += r.amount
        g["gross"] += abs(r.amount)
        g["_weight"][default_bucket_for_cat(r.cat)] += abs(r.amount)
        g["_catw"][r.cat] += abs(r.amount)
    groups = []
    for g in agg.values():
        weights = g.pop("_weight")
        catw = g.pop("_catw")
        dominant_cat = max(catw, key=catw.get) if catw else ""
        default = max(weights, key=weights.get) if weights else "spending"
        # Venmo/gambling groups are netted automatically — not user-mappable.
        g["auto"] = dominant_cat in NETTING_CATS
        g["default_bucket"] = default
        # mapped = the user has already sorted this group (its key is saved).
        # Lets the UI prompt only for net-new groups on a re-import.
        g["mapped"] = g["key"] in GROUP_BUCKET_MAP
        g["bucket"] = "auto" if g["auto"] else GROUP_BUCKET_MAP.get(g["key"], default)
        groups.append(g)
    # Biggest movers first so the user sorts the impactful groups up top.
    groups.sort(key=lambda x: -x["gross"])
    return groups


# ──────────────────────────────────────────────────────────────────────────────
# Computation
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Result:
    as_of: date
    year_start: date
    year_end: date
    months_ytd: float
    months_remaining: float

    savings_excl_401k: float
    savings_incl_401k: float
    spending: float
    k401_ytd: float

    proj_savings_incl_401k: float
    proj_spending: float

    # Components for narrative breakdown
    rws_net: float = 0.0
    td_net: float = 0.0
    main_accum: float = 0.0
    net_venmo: float = 0.0
    spend_main_net: float = 0.0
    biweek_deposits_ytd: float = 0.0
    direct_exp: float = 0.0
    rent_direct: float = 0.0
    venmo_spend: float = 0.0
    gamb_spend: float = 0.0
    external_income: float = 0.0
    assumed_rent: float = 0.0  # January rent prepaid prior Dec (out of window)

    # Projection methodology
    proj_spending_flat: float = 0.0     # naive run-rate × months_remaining
    proj_spending_seasonal: float = 0.0 # adjusted using prior_years/ data
    seasonal_factor_used: float = 1.0
    seasonality: list = field(default_factory=list)  # list[YearSeasonality]
    rent_monthly: float = 0.0

    breakdown_savings: dict = field(default_factory=dict)
    breakdown_spending: dict = field(default_factory=dict)
    unclassified: list = field(default_factory=list)


# Paycheck size at/above which a deposit is treated as a one-time bonus.
# Personal (it hints at salary), so it's loaded from finance_config.json at
# runtime and set on this module global in main() before any computation.
BONUS_THRESHOLD = None
# Fixed threshold used only for the prior-year seasonal calculation, so the
# seasonal-lift factor stays independent of the user's bonus setting (which
# should affect the current year only).
SEASONAL_BONUS_THRESHOLD = 15000.0
PAYCHECKS_PER_YEAR = 26
# User-defined mapping of transaction group -> financial bucket
# ("spending" | "savings" | "income"), set in main() from finance_config.json's
# "group_buckets". It's the successor to the old account-last-4/name matching:
# instead of typing account numbers, the user sorts each payee/destination group
# into a column in the ingest mapping screen. Applied in load() by remapping each
# row's category, so compute()/compute_monthly()/build_primitives() and all the
# mirrored client-side JS stay category-driven and unchanged.
GROUP_BUCKET_MAP: dict = {}

# Natural bucket of each fine category — used to seed the mapping screen's smart
# defaults and to tell when a user override actually differs from the default.
CAT_BUCKET = {
    "SPEND_MAIN": "spending", "DIRECT_EXP": "spending", "RENT": "spending",
    "VENMO_OUT": "spending", "GAMBLING_OUT": "spending",
    "INVEST_WEALTHFRONT": "savings", "INVEST_ROBINHOOD": "savings",
    "SAVINGS_XFER": "savings", "TD_XFER": "savings",
    "PAYROLL": "income", "TAX_REFUND": "income", "INTEREST": "income",
    "ATM_REIMB": "income", "OTHER_IN": "income", "SPLITWISE": "income",
    "VENMO_IN": "income", "GAMBLING_IN": "income",
    "UNCLASSIFIED": "spending",   # an unrecognized outflow reads as spending
}
# When a user override moves a row off its natural bucket, remap to a generic
# category that sums into that bucket with no special (rent-shift / bonus /
# netting) behavior, so only the deliberately-moved rows change.
BUCKET_GENERIC = {"savings": "SAVINGS_XFER", "spending": "DIRECT_EXP",
                  "income": "OTHER_IN"}
# Venmo/gambling net inflows against outflows (governed by the net-venmo /
# net-gambling conventions), so a single spending/savings/income bucket can't
# represent them without breaking the wash. They're handled automatically and
# shown read-only in the mapping screen.
NETTING_CATS = {"VENMO_IN", "VENMO_OUT", "GAMBLING_IN", "GAMBLING_OUT"}


def group_key(desc: str) -> str:
    """Collapse a raw description to a stable payee/destination key so per-row
    noise (dates, amounts, reference/store numbers) doesn't split one payee into
    many groups. A trailing 'account xxxxxx1234' suffix is preserved as ' #1234'
    so distinct transfer destinations stay distinct (this replaces the old
    account-last-4 matching)."""
    d = re.sub(r"\s+", " ", desc.strip().lower())
    m = re.search(r"account x*(\d{4})\b", d)
    acct = m.group(1) if m else None
    d = re.sub(r"x{2,}\d+", " ", d)                               # masked numbers
    d = re.sub(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", " ", d)   # dates
    d = re.sub(r"[#*]?\d{3,}", " ", d)                            # ref/store nums
    d = re.sub(r"[^a-z ]+", " ", d)                               # punctuation
    tokens = re.sub(r"\s+", " ", d).strip().split()
    key = " ".join(tokens[:5]) if tokens else "misc"
    return f"{key} #{acct}" if acct else key


def group_label(desc: str) -> str:
    """Short, human-friendly title for a group card in the mapping screen."""
    k = group_key(desc)
    k = re.sub(r" #(\d{4})$", lambda mm: " …" + mm.group(1), k)
    # Trim verbose bank phrasing so the meaningful part (incl. the …#### suffix)
    # stays visible on the card instead of being truncated away.
    k = k.replace("internet transfer ", "transfer ")
    k = re.sub(r"\b(spending|savings) account\b", r"\1", k)
    return k[:1].upper() + k[1:] if k else k


def default_bucket_for_cat(cat: str) -> str:
    return CAT_BUCKET.get(cat, "spending")


def apply_group_bucket(cat: str, key: str) -> str:
    """Return the effective category after applying any user bucket override for
    this group. Keeps the fine category (and its special handling) when the
    chosen bucket matches the category's natural bucket; otherwise remaps to a
    generic category for the chosen bucket."""
    if cat in NETTING_CATS:
        return cat                       # netted automatically; never remapped
    chosen = GROUP_BUCKET_MAP.get(key)
    if cat == "UNCLASSIFIED":
        # An unrecognized outflow only touches main_accum, which would leave the
        # savings/spending identity short. Resolve it into a real bucket —
        # spending by default — so nothing is silently uncounted.
        return BUCKET_GENERIC.get(chosen or "spending", "DIRECT_EXP")
    if not chosen or chosen == default_bucket_for_cat(cat):
        return cat
    return BUCKET_GENERIC.get(chosen, cat)
_HERE = os.path.dirname(os.path.abspath(__file__))
# Writable in a distributed app (user can drop <YYYY>_transactions.csv here);
# same repo-local path as before when running from source.
DEFAULT_PRIOR_YEARS_DIR = os.path.join(app_paths.data_dir(), "prior_years")

# Personal baseline figures live in a machine-local, git-ignored config file
# (never committed) so the numbers aren't tied to whatever the last person
# pushed to the remote. It's populated by the dashboard's first-run onboarding
# UI (which POSTs to the local bridge), never in the terminal. See
# finance_config.example.json for the schema.
CONFIG_PATH = os.path.join(app_paths.data_dir(), "finance_config.json")


def load_local_config() -> dict:
    """Read the machine-local personal config, or {} if it doesn't exist yet."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _setup_message() -> str:
    """Markdown shown on stdout when no baseline config exists yet — the numbers
    are gathered through the dashboard, so there's nothing to print here."""
    return (
        "## First-time setup\n\n"
        "No personal baseline values are configured on this machine yet, so the "
        "dashboard was generated in **setup mode**. Open it and answer a few "
        "quick questions (monthly housing expense, other W-2 accounts, 401k, "
        "bonuses). Your answers save locally to `finance_config.json` — no "
        "terminal entry needed — and the projections populate instantly.\n\n"
        "_Re-run `/finproject` afterward to also get the text summary here._"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Historical seasonality
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class YearSeasonality:
    year: int
    source_file: str
    rent_monthly: float
    janmay_nonrent_monthly: float
    jundec_nonrent_monthly: float
    seasonal_factor: float        # jundec / janmay
    sample_months_janmay: int     # how many of Jan-May had transactions
    sample_months_jundec: int


def _detect_year_rent(year_rows: list[Row]) -> float | None:
    """Infer monthly rent for a year from its HRV (or similar) direct
    rent withdrawals. Returns None if no rent rows present."""
    rent_amts = [round(-r.amount, 2) for r in year_rows if r.cat == "RENT"]
    if not rent_amts:
        return None
    return Counter(rent_amts).most_common(1)[0][0]


def _period_spending(year_rows: list[Row], biweekly_deposit: float) -> float:
    """Total spending for a slice of rows, mirroring the main compute()
    rules: spending-account net + biweekly + direct + rent + gambling, Venmo netted.

    Uses the fixed SEASONAL_BONUS_THRESHOLD (not the user's BONUS_THRESHOLD) so
    the prior-year seasonal factor doesn't move when the user changes their
    bonus setting."""
    sums = defaultdict(float)
    n_pay = 0
    for r in year_rows:
        sums[r.cat] += r.amount
        if r.cat == "PAYROLL" and r.amount < SEASONAL_BONUS_THRESHOLD:
            n_pay += 1
    spend_main = -sums["SPEND_MAIN"]
    biweek_main = biweekly_deposit * n_pay
    direct = -sums["DIRECT_EXP"]
    rent = -sums["RENT"]
    gamb = -sums["GAMBLING_OUT"]
    return spend_main + biweek_main + direct + rent + gamb


def load_historical(
    prior_years_dir: str,
    biweekly_deposit: float,
) -> list[YearSeasonality]:
    """Load every CSV in prior_years_dir, compute per-year seasonality."""
    out = []
    if not os.path.isdir(prior_years_dir):
        return out
    for path in sorted(glob.glob(os.path.join(prior_years_dir, "*.csv"))):
        rows = load(path)
        if not rows:
            continue
        years_present = sorted(set(r.date.year for r in rows))
        for y in years_present:
            year_rows = [r for r in rows if r.date.year == y]
            rent = _detect_year_rent(year_rows)
            if rent is None:
                # No detectable rent — skip this year (can't normalize cleanly).
                continue
            jm = [r for r in year_rows if r.date.month <= 5]
            jd = [r for r in year_rows if r.date.month >= 6]
            jm_months = len(set(r.date.month for r in jm))
            jd_months = len(set(r.date.month for r in jd))
            if jm_months == 0 or jd_months == 0:
                continue
            jm_spend = _period_spending(jm, biweekly_deposit)
            jd_spend = _period_spending(jd, biweekly_deposit)
            jm_nonrent = jm_spend - rent * jm_months
            jd_nonrent = jd_spend - rent * jd_months
            if jm_nonrent <= 0:
                continue
            jm_monthly = jm_nonrent / jm_months
            jd_monthly = jd_nonrent / jd_months
            out.append(YearSeasonality(
                year=y, source_file=os.path.basename(path),
                rent_monthly=rent,
                janmay_nonrent_monthly=jm_monthly,
                jundec_nonrent_monthly=jd_monthly,
                seasonal_factor=jd_monthly / jm_monthly,
                sample_months_janmay=jm_months,
                sample_months_jundec=jd_months,
            ))
    return out


def compute(
    rows: list[Row],
    as_of: date,
    k401_annual: float,
    biweekly_deposit: float,
    net_venmo: bool,
    net_gambling: bool,
    td_as_savings: bool,
    rent_monthly: float = 0.0,
    seasonality: list[YearSeasonality] | None = None,
) -> Result:
    year_start = date(as_of.year, 1, 1)
    year_end = date(as_of.year, 12, 31)
    months_ytd = ((as_of - year_start).days + 1) / 30.4375
    months_remaining = (year_end - as_of).days / 30.4375

    sums = defaultdict(float)
    counts = defaultdict(int)
    payroll_rows = []
    for r in rows:
        if r.date < year_start or r.date > as_of:
            continue
        sums[r.cat] += r.amount
        counts[r.cat] += 1
        if r.cat == "PAYROLL":
            payroll_rows.append(r)

    # Payroll: separate bonus from regular
    regular_pay = [r for r in payroll_rows if r.amount < BONUS_THRESHOLD]
    bonus = sum(r.amount for r in payroll_rows if r.amount >= BONUS_THRESHOLD)
    n_regular = len(regular_pay)

    # 401k YTD (linear assumption)
    k401_ytd = (n_regular / PAYCHECKS_PER_YEAR) * k401_annual

    # ----- Savings components -----
    rws_net = -(sums["INVEST_WEALTHFRONT"] + sums["INVEST_ROBINHOOD"]
                + sums["SAVINGS_XFER"])  # convert outflow sign to positive
    td_net = -sums["TD_XFER"] if td_as_savings else 0.0

    # Main-account accumulation = sum of all signed amounts (deposits +,
    # withdrawals -). Captures Venmo wash, splitwise reimbursements, etc.
    main_accum = sum(r.amount for r in rows
                     if year_start <= r.date <= as_of)

    savings_excl_401k = rws_net + td_net + main_accum
    savings_incl_401k = savings_excl_401k + k401_ytd

    breakdown_savings = {
        "R/W/S net out": rws_net,
        "TD net out (savings)": td_net,
        "Main account accumulation": main_accum,
        "401k YTD (linear)": k401_ytd,
    }

    # ----- Spending components -----
    spend_main_net = -sums["SPEND_MAIN"]
    # Biweekly direct deposit into the spending account (assumed fully spent)
    biweek_deposits_ytd = biweekly_deposit * n_regular
    direct_exp = -sums["DIRECT_EXP"]
    rent_direct = -sums["RENT"]  # rent paid directly from main (e.g. HRV 2025)
    td_spend = 0.0 if td_as_savings else -sums["TD_XFER"]

    venmo_out_gross = -sums["VENMO_OUT"]
    venmo_in_gross = sums["VENMO_IN"]
    venmo_net = venmo_in_gross - venmo_out_gross  # positive = net inflow
    venmo_spend = 0.0 if net_venmo else venmo_out_gross

    gamb_out_gross = -sums["GAMBLING_OUT"]
    gamb_in_gross = sums["GAMBLING_IN"]
    gamb_net = gamb_in_gross - gamb_out_gross
    gamb_spend = 0.0 if net_gambling else gamb_out_gross

    spending = (spend_main_net + biweek_deposits_ytd + direct_exp
                + rent_direct + venmo_spend + gamb_spend + td_spend)

    # January's rent is prepaid the prior December, which falls outside a Jan–Dec
    # window, so it never lands in captured spend. Assume it was paid in full and
    # surface it in the YTD spending total. Skip if January already holds an
    # in-month rent-sized payment (days 1–20 — i.e. paid for January itself, not
    # a late-month prepay for February).
    jan_has_own_rent = any(
        r.cat == "SPEND_MAIN" and r.date.month == 1 and r.date.day < 21
        and -r.amount >= rent_monthly
        for r in rows if year_start <= r.date <= as_of
    )
    assumed_rent = (rent_monthly if (rent_monthly and not jan_has_own_rent
                                     and as_of >= year_start) else 0.0)

    breakdown_spending = {
        f"Spending account (net + ${biweekly_deposit:.0f} biweekly × {n_regular})":
            spend_main_net + biweek_deposits_ytd,
        "  · net main → spending": spend_main_net,
        f"  · ${biweekly_deposit:.0f} biweekly direct deposit (assumed spent)":
            biweek_deposits_ytd,
        "Direct expenses from main": direct_exp,
        f"Venmo OUT ({'netted to wash' if net_venmo else 'gross'})": venmo_spend,
        f"Gambling OUT ({'netted to wash' if net_gambling else 'gross'})": gamb_spend,
        f"TD checking ({'savings' if td_as_savings else 'spending'})": td_spend,
    }

    # ----- Projection -----
    # Strip one-time items (bonus, TD) before projecting savings monthly rate
    sav_recurring = savings_excl_401k - bonus - td_net
    sav_monthly = sav_recurring / months_ytd if months_ytd else 0
    sav_remaining = sav_monthly * months_remaining
    proj_savings_excl_401k_flat = savings_excl_401k + sav_remaining

    sp_monthly = spending / months_ytd if months_ytd else 0
    sp_remaining_flat = sp_monthly * months_remaining
    proj_spending_flat = spending + sp_remaining_flat

    # Seasonal projection: only apply if (a) we have rent info, (b) we have
    # at least one historical year, and (c) the projection period extends
    # into Jun-Dec months not yet captured in YTD.
    seasonal_factor = 1.0
    proj_spending_seasonal = proj_spending_flat
    if rent_monthly > 0 and seasonality:
        factors = [s.seasonal_factor for s in seasonality]
        seasonal_factor = sum(factors) / len(factors)
        # Split remaining months into Jan-May vs Jun-Dec buckets.
        days_remaining_janmay = 0
        days_remaining_jundec = 0
        d = as_of + timedelta(days=1)
        while d <= year_end:
            if d.month <= 5:
                days_remaining_janmay += 1
            else:
                days_remaining_jundec += 1
            d += timedelta(days=1)
        mo_jm_remain = days_remaining_janmay / 30.4375
        mo_jd_remain = days_remaining_jundec / 30.4375

        # YTD non-rent monthly (based on YTD spending minus rent run-rate).
        ytd_nonrent = spending - rent_monthly * months_ytd
        ytd_nonrent_monthly = ytd_nonrent / months_ytd if months_ytd else 0

        # If YTD is entirely Jan-May, use that as the "Jan-May baseline"
        # and apply the seasonal_factor to project Jun-Dec.
        # If YTD is already into Jun-Dec, use the YTD itself as the seasonal
        # baseline (no lift — we're already in the lifted period).
        # YTD (even when as_of is past May) is dominated by Jan-May months, so
        # treat ytd_nonrent_monthly as the un-lifted Jan-May baseline and apply
        # the seasonal_factor to the remaining Jun-Dec months. (Before, the
        # as_of.month > 5 branch skipped the lift entirely, silently collapsing
        # the seasonal projection back to the flat run-rate.)
        jundec_nonrent_remaining = (ytd_nonrent_monthly
                                     * seasonal_factor * mo_jd_remain)
        janmay_nonrent_remaining = ytd_nonrent_monthly * mo_jm_remain

        rent_remaining = rent_monthly * months_remaining
        sp_remaining_seasonal = (jundec_nonrent_remaining
                                  + janmay_nonrent_remaining + rent_remaining)
        proj_spending_seasonal = spending + sp_remaining_seasonal

    proj_spending = proj_spending_seasonal  # default reported number

    # Savings projection: stay consistent with spending projection. Any extra
    # spending vs. flat run-rate has to come out of savings (income is fixed).
    seasonal_spending_lift = proj_spending_seasonal - proj_spending_flat
    proj_savings_excl_401k = proj_savings_excl_401k_flat - seasonal_spending_lift
    proj_savings_incl_401k = proj_savings_excl_401k + k401_annual

    unclassified = [(r.date.isoformat(), r.amount, r.desc)
                    for r in rows if r.cat == "UNCLASSIFIED"
                    and year_start <= r.date <= as_of]

    # External income (for identity check): sources of money truly external to
    # the user's accounts. Excludes internal transfers (R/W/S → main, spending → main).
    if net_venmo:
        venmo_income = max(venmo_net, 0.0)
    else:
        venmo_income = venmo_in_gross
    if net_gambling:
        gamb_income = max(gamb_net, 0.0)
    else:
        gamb_income = gamb_in_gross

    external_income = (
        sums["PAYROLL"]
        + sums["TAX_REFUND"]
        + sums["INTEREST"]
        + sums["ATM_REIMB"]
        + sums["OTHER_IN"]
        + sums["SPLITWISE"]
        + venmo_income
        + gamb_income
        + biweek_deposits_ytd
    )

    return Result(
        as_of=as_of, year_start=year_start, year_end=year_end,
        months_ytd=months_ytd, months_remaining=months_remaining,
        savings_excl_401k=savings_excl_401k,
        savings_incl_401k=savings_incl_401k,
        spending=spending,
        k401_ytd=k401_ytd,
        proj_savings_incl_401k=proj_savings_incl_401k,
        proj_spending=proj_spending,
        rws_net=rws_net, td_net=td_net, main_accum=main_accum,
        net_venmo=venmo_net,
        spend_main_net=spend_main_net,
        biweek_deposits_ytd=biweek_deposits_ytd,
        direct_exp=direct_exp,
        rent_direct=rent_direct,
        venmo_spend=venmo_spend,
        gamb_spend=gamb_spend,
        external_income=external_income,
        assumed_rent=assumed_rent,
        proj_spending_flat=proj_spending_flat,
        proj_spending_seasonal=proj_spending_seasonal,
        seasonal_factor_used=seasonal_factor,
        seasonality=seasonality or [],
        rent_monthly=rent_monthly,
        breakdown_savings=breakdown_savings,
        breakdown_spending=breakdown_spending,
        unclassified=unclassified,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────────────────────────────────────
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _d(amt: float) -> str:
    """Currency, no decimals, with grouping."""
    if abs(amt) < 0.5:
        amt = 0.0
    return f"${amt:,.0f}"


def _dc(amt: float) -> str:
    """Currency with cents."""
    if abs(amt) < 0.005:
        amt = 0.0
    return f"${amt:,.2f}"


def render(res: Result) -> str:
    as_of = res.as_of
    as_of_label = f"{MONTHS[as_of.month-1]} {as_of.day}, {as_of.year}"
    ytd_label = f"Jan 1 – {MONTHS[as_of.month-1]} {as_of.day}, {as_of.year}"

    L = []
    L.append(f"## YTD ({ytd_label})")
    L.append("")
    L.append("| # | Metric | Amount |")
    L.append("|---|---|---|")
    L.append(f"| 1 | **Savings to date (excl 401k)** | **{_dc(res.savings_excl_401k)}** |")
    L.append(f"| 2 | **Savings to date (incl 401k)** | **{_dc(res.savings_incl_401k)}** |")
    spend_total = res.spending + res.assumed_rent
    L.append(f"| 3 | **Post-tax spending to date** | **{_dc(spend_total)}** |")
    L.append("")

    L.append(f"## Projected to Dec 31, {res.year_end.year}")
    L.append("")
    L.append("| # | Metric | Amount |")
    L.append("|---|---|---|")
    L.append(f"| 4 | **Projected savings by 12/31 (incl 401k)** | **~{_d(res.proj_savings_incl_401k)}** |")
    L.append(f"| 5 | **Projected post-tax spending by 12/31** | **~{_d(res.proj_spending)}** |")
    L.append("")
    L.append("---")
    L.append("")

    venmo_note = (f" *(includes {'+' if res.net_venmo >= 0 else ''}{_dc(res.net_venmo)} net Venmo wash)*"
                  if abs(res.net_venmo) > 0.01 else "")
    L.append(f"**Savings {_dc(res.savings_excl_401k)} breakdown:**")
    L.append(f"- R/W/S net out (Robinhood + Wealthfront + savings accounts): {_dc(res.rws_net)}")
    L.append(f"- TD checking net out (savings vehicle): {_dc(res.td_net)}")
    L.append(f"- Main account net accumulation: {_dc(res.main_accum)}{venmo_note}")
    L.append(f"- 401k YTD (linear across 26 paychecks): {_dc(res.k401_ytd)}")
    L.append("")

    L.append(f"**Spending {_dc(spend_total)} breakdown:**")
    L.append(f"- Spending account (net {_dc(res.spend_main_net)} + "
             f"{_dc(res.biweek_deposits_ytd)} biweekly direct deposit): "
             f"{_dc(res.spend_main_net + res.biweek_deposits_ytd)}")
    L.append(f"- Direct expenses from main: {_dc(res.direct_exp)}")
    if res.rent_direct:
        L.append(f"- Housing paid directly from main (HRV): {_dc(res.rent_direct)}")
    if res.venmo_spend:
        L.append(f"- Venmo OUT (gross): {_dc(res.venmo_spend)}")
    if res.gamb_spend:
        L.append(f"- Gambling OUT (gross): {_dc(res.gamb_spend)}")
    if res.assumed_rent:
        L.append(f"- Assumed January housing (prepaid prior Dec, out of window): "
                 f"{_dc(res.assumed_rent)}")
    L.append("")

    # Add the assumed January rent to both sides: it was funded by the prior
    # December's paycheck (income received out of window), so it reconciles as
    # extra income offsetting the extra spending — savings is unchanged.
    income_total = res.external_income + res.assumed_rent
    identity_sum = res.savings_excl_401k + spend_total
    ok = "✓" if abs(income_total - identity_sum) < 1.0 else "⚠️ MISMATCH"
    income_note = (f" (incl {_dc(res.assumed_rent)} assumed prepaid housing)"
                   if res.assumed_rent else "")
    L.append(f"**Identity check:** {_dc(income_total)} external income{income_note} = "
             f"{_dc(res.savings_excl_401k)} savings + {_dc(spend_total)} spending {ok}")
    L.append("")

    if res.seasonality:
        L.append("**Projection methodology:**")
        L.append(f"- Flat run-rate: spending → {_d(res.proj_spending_flat)} "
                 f"(extrapolates YTD monthly pace)")
        L.append(f"- Seasonal-adjusted (used above): spending → "
                 f"{_d(res.proj_spending_seasonal)} "
                 f"(applies {res.seasonal_factor_used:.3f}× lift to Jun–Dec "
                 f"non-housing spending, based on prior-year history)")
        L.append(f"- Current-year housing assumed: {_dc(res.rent_monthly)}/mo")
        L.append("")
        L.append("**Historical seasonality from `prior_years/`:**")
        for s in res.seasonality:
            L.append(f"- **{s.year}** ({s.source_file}): housing {_dc(s.rent_monthly)}/mo · "
                     f"Jan–May non-housing {_dc(s.janmay_nonrent_monthly)}/mo → "
                     f"Jun–Dec {_dc(s.jundec_nonrent_monthly)}/mo · "
                     f"factor **{s.seasonal_factor:.3f}**")
        L.append("")
    else:
        L.append(f"_Projection method: flat run-rate. Drop CSVs in "
                 f"`finance-projector/prior_years/` to enable seasonal adjustment._")
        L.append("")

    L.append(f"_As of {as_of_label} · Months YTD: {res.months_ytd:.2f} · "
             f"Months remaining: {res.months_remaining:.2f}_")

    if res.unclassified:
        L.append("")
        L.append("⚠️ **Unclassified rows** (add rules in `classify()`):")
        for d, a, desc in res.unclassified:
            L.append(f"- `{d}` {_dc(a)} — {desc[:80]}")

    return "\n".join(L)


# ──────────────────────────────────────────────────────────────────────────────
# HTML dashboard
# ──────────────────────────────────────────────────────────────────────────────
def compute_monthly(
    rows: list[Row],
    as_of: date,
    k401_annual: float,
    biweekly_deposit: float,
    net_venmo: bool,
    net_gambling: bool,
    td_as_savings: bool,
    rent_monthly: float = 0.0,
) -> dict:
    """Per-month savings + spending for the dashboard, mirroring compute()."""
    year_start = date(as_of.year, 1, 1)
    main_accum = defaultdict(float)
    rws = defaultdict(float)
    td_xfer = defaultdict(float)
    pay = defaultdict(int)
    spend_main = defaultdict(float)
    direct = defaultdict(float)
    gamb_out = defaultdict(float)
    venmo_out = defaultdict(float)
    rent_direct = defaultdict(float)

    for r in rows:
        if r.date < year_start or r.date > as_of:
            continue
        m = r.date.month
        main_accum[m] += r.amount
        if r.cat in ("INVEST_WEALTHFRONT", "INVEST_ROBINHOOD", "SAVINGS_XFER"):
            rws[m] += -r.amount
        elif r.cat == "TD_XFER":
            td_xfer[m] += -r.amount
        elif r.cat == "SPEND_MAIN":
            # Rent is prepaid: a large transfer (>= a month's rent) on/after the
            # 21st is treated as the *following* month's rent. Only one month's
            # rent is shifted to that month; any excess stays as this month's
            # discretionary spend. Only shift when the next month is itself
            # within the data window (never move spend into a projected month).
            amt = -r.amount
            nxt = (date(r.date.year, m + 1, 1) if m < 12 else None)
            if (rent_monthly and amt >= rent_monthly and r.date.day >= 21
                    and nxt is not None and nxt <= as_of):
                spend_main[m + 1] += rent_monthly
                spend_main[m] += amt - rent_monthly
            else:
                spend_main[m] += amt
        elif r.cat == "DIRECT_EXP":
            direct[m] += -r.amount
        elif r.cat == "GAMBLING_OUT":
            gamb_out[m] += -r.amount
        elif r.cat == "VENMO_OUT":
            venmo_out[m] += -r.amount
        elif r.cat == "RENT":
            rent_direct[m] += -r.amount
        if r.cat == "PAYROLL" and r.amount < BONUS_THRESHOLD:
            pay[m] += 1

    per_pay = k401_annual / PAYCHECKS_PER_YEAR
    excl, k401v, incl = [], [], []
    actual, expected, paychecks = [], [], []
    for m in range(1, 13):
        td_sav = td_xfer[m] if td_as_savings else 0.0
        e = round(main_accum[m] + rws[m] + td_sav, 2)
        k = round(pay[m] * per_pay, 2)
        excl.append(e)
        k401v.append(k)
        incl.append(round(e + k, 2))

        venmo_spend = 0.0 if net_venmo else venmo_out[m]
        gamb_spend = 0.0 if net_gambling else gamb_out[m]
        td_spend = 0.0 if td_as_savings else td_xfer[m]
        a = round(spend_main[m] + biweekly_deposit * pay[m] + direct[m]
                  + rent_direct[m] + venmo_spend + gamb_spend + td_spend, 2)
        actual.append(a)
        expected.append(round(biweekly_deposit * pay[m], 2))
        paychecks.append(pay[m])

    data_months = sorted(set(r.date.month for r in rows
                             if year_start <= r.date <= as_of))
    last = max(data_months) if data_months else 0

    # Project future paycheck dates by continuing the biweekly (14-day) cadence
    # from the last actual payroll, so a month with a 3rd pay period (twice a
    # year) shows its income/401k bump instead of a flat 2/month assumption.
    payroll_dates = sorted(r.date for r in rows
                           if r.cat == "PAYROLL" and r.amount < BONUS_THRESHOLD
                           and year_start <= r.date <= as_of)
    proj_paychecks = [0] * 12
    if payroll_dates:
        year_end = date(as_of.year, 12, 31)
        d = payroll_dates[-1] + timedelta(days=14)
        while d <= year_end:
            # Count every projected paycheck after as_of, including any that
            # fall in the rest of the current (partial) month — those feed the
            # mid-month projected-remainder cap in build_panel().
            if d > as_of:
                proj_paychecks[d.month - 1] += 1
            d += timedelta(days=14)

    return {
        "excl": excl, "k401": k401v, "incl": incl,
        "actual": actual, "expected": expected, "paychecks": paychecks,
        "proj_paychecks": proj_paychecks,
        "last_month": last,
    }


# Token-substituted template. Render identical output to the hand-built file;
# %%TOKENS%% are filled from the live run. JS uses a raw string so "\n" survives.
# Each year is a "panel"; PANELS_JSON carries one entry per tab and the JS builds
# the cards/charts/table for each, with a tab bar to switch between them.
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FinProject</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/favicon.png">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --bg:#0f1117; --card:#1a1d27; --text:#e6e8ee; --muted:#9aa0ad;
          --accent:#7c5cff; --accent2:#39d3bb; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
         Roboto,Helvetica,Arial,sans-serif; background:var(--bg);
         color:var(--text); padding:32px; }
  .wrap { max-width:960px; margin:0 auto; }
  h1 { font-size:24px; margin:0 0 4px; }
  .title-row { display:flex; align-items:center; gap:11px; margin-bottom:4px; }
  .title-row h1 { margin:0; }
  .logo { height:40px; width:auto; flex:none; }
  .sub { color:var(--muted); margin:0 0 24px; font-size:14px; }
  .tabs { display:flex; gap:8px; margin-bottom:24px; flex-wrap:wrap; }
  .tab-btn { background:var(--card); color:var(--muted); border:1px solid #262a36;
             border-radius:10px; padding:8px 18px; cursor:pointer; font-size:14px;
             font-weight:600; }
  .tab-btn:hover { color:var(--text); }
  .tab-btn.active { color:var(--text); border-color:var(--accent);
                    background:#211f3a; }
  .subtabs { display:flex; gap:8px; margin:-16px 0 24px; flex-wrap:wrap; }
  .subtabs .tab-btn { padding:6px 15px; font-size:13px; }
  .panel { display:none; }
  .panel.active { display:block; }
  .cards { display:flex; gap:16px; margin-bottom:24px; flex-wrap:wrap; }
  .card { background:var(--card); border-radius:14px; padding:18px 22px;
          flex:1; min-width:200px; border:1px solid #262a36; }
  .card .lbl { color:var(--muted); font-size:13px; margin-bottom:6px; }
  .card .val { font-size:26px; font-weight:700; }
  .chart-card { background:var(--card); border-radius:14px; padding:22px;
                border:1px solid #262a36; }
  .spend-legend { display:flex; flex-wrap:wrap; justify-content:center;
                  gap:22px; margin-bottom:12px; font-size:13px; color:#e6e8ee; }
  .spend-legend .lg-item { display:inline-flex; align-items:center; gap:8px; }
  .spend-legend .lg-box { width:24px; height:13px; border-radius:3px; }
  .spend-legend .lg-teal { background:#39d3bb; }
  .spend-legend .lg-purple { background:#7c5cff; }
  .spend-legend .lg-line { position:relative; width:28px; height:13px; }
  .spend-legend .lg-line::before { content:''; position:absolute; top:50%;
                  left:0; right:0; border-top:2px dashed #39d3bb;
                  transform:translateY(-50%); }
  .spend-legend .lg-dot { position:absolute; top:50%; left:50%; width:7px;
                  height:7px; border-radius:50%; background:#39d3bb;
                  transform:translate(-50%,-50%); }
  table { width:100%; border-collapse:collapse; margin-top:24px;
          background:var(--card); border-radius:14px; overflow:hidden;
          border:1px solid #262a36; table-layout:fixed; }
  th:first-child,td:first-child { width:25%; }
  th,td { padding:10px 14px; text-align:right; font-size:14px;
          border-bottom:1px solid #262a36; }
  th:first-child,td:first-child { text-align:left; }
  thead th { color:var(--muted); font-weight:600; background:#161922; }
  tbody tr:last-child td { border-bottom:none; }
  tfoot td { font-weight:700; background:#161922; }
  tbody tr.row-hover td { background:rgba(124,92,255,0.22); }
  tfoot tr.foot-hover td { background:rgba(57,211,187,0.20); color:var(--accent2); }
  .pos { color:var(--accent2); }
  .neg { color:#ff6b6b; }
  .foot { color:var(--muted); font-size:12px; margin-top:18px; }
  h2 { font-size:17px; margin:28px 0 10px; font-weight:600; }
  .val.proj { color:var(--accent); }
  /* Settings gear + modal */
  .gear-btn { position:fixed; top:20px; right:20px; z-index:50; width:44px; height:44px;
              border-radius:50%; background:var(--card); border:1px solid #262a36;
              color:var(--muted); font-size:20px; line-height:1; cursor:pointer;
              display:flex; align-items:center; justify-content:center;
              transition:color .15s, border-color .15s, transform .3s; }
  .gear-btn:hover { color:var(--text); border-color:var(--accent); transform:rotate(45deg); }
  .import-btn { right:74px; font-size:17px; }
  .import-btn:hover { transform:none; }
  .power-btn { position:fixed; top:20px; left:20px; z-index:50; width:44px; height:44px;
               border-radius:50%; background:var(--card); border:1px solid #262a36;
               color:var(--muted); cursor:pointer; display:flex; align-items:center;
               justify-content:center; transition:color .15s, border-color .15s; }
  .power-btn:hover { color:#ff6b6b; border-color:#ff6b6b; }
  .power-btn svg { width:20px; height:20px; }
  .reset-btn { position:fixed; top:20px; left:74px; z-index:50; width:44px; height:44px;
               border-radius:50%; background:var(--card); border:1px solid #262a36;
               color:var(--muted); cursor:pointer; display:flex; align-items:center;
               justify-content:center; transition:color .15s, border-color .15s; }
  .reset-btn:hover { color:#f0a500; border-color:#f0a500; }
  .reset-btn svg { width:20px; height:20px; }
  .modal .drop { border:2px dashed #33384a; border-radius:12px; padding:26px 16px;
                 text-align:center; color:var(--muted); cursor:pointer; background:var(--bg);
                 font-size:13px; transition:border-color .15s, color .15s; }
  .modal .drop.over { border-color:var(--accent); color:var(--text); }
  .modal .drop.has-file { border-style:solid; border-color:var(--accent); color:var(--text); }
  .modal .filechip { display:inline-flex; align-items:center; gap:11px; background:var(--card);
                     border:1px solid #33384a; border-radius:10px; padding:11px 15px; max-width:100%; }
  .modal .filechip svg { flex:none; }
  .modal .fc-name { font-size:14px; font-weight:600; color:var(--text); word-break:break-all; text-align:left; }
  .modal .fc-hint { margin-top:11px; font-size:12px; color:var(--muted); }
  .modal textarea { width:100%; margin-top:12px; background:var(--bg); border:1px solid #262a36;
                    border-radius:8px; color:var(--text); padding:10px; font-size:12px;
                    font-family:ui-monospace,Menlo,monospace; resize:vertical; }
  .modal textarea:focus { outline:none; border-color:var(--accent); }
  .modal-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:100;
                   display:none; align-items:flex-start; justify-content:center;
                   padding:60px 20px; overflow-y:auto; }
  .modal-overlay.open { display:flex; }
  .modal { background:var(--card); border:1px solid #262a36; border-radius:16px;
           padding:28px; width:100%; max-width:420px; }
  .modal h2 { margin:0 0 4px; font-size:20px; }
  .modal .msub { color:var(--muted); font-size:13px; margin:0 0 22px; }
  .modal .section-label { font-size:12px; font-weight:600; letter-spacing:.04em;
           text-transform:uppercase; color:var(--muted); margin:26px 0 4px;
           padding-top:20px; border-top:1px solid #262a36; }
  .modal .section-label + .msub { margin:0 0 16px; }
  .field { margin-bottom:18px; }
  .field label { display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }
  .field .hint { font-size:11px; color:var(--muted); margin-top:5px; }
  .input-prefix { position:relative; }
  .input-prefix > span { position:absolute; left:13px; top:50%;
                         transform:translateY(-50%); color:var(--muted); font-size:15px; }
  .modal .field .input-prefix input { padding-left:30px; }
  .field input[type=number], .field input[type=text] {
           width:100%; background:var(--bg); border:1px solid #262a36;
           border-radius:8px; color:var(--text); padding:10px 12px; font-size:15px; }
  .field input[type=number]:focus, .field input[type=text]:focus {
           outline:none; border-color:var(--accent); }
  .field.inline { display:flex; align-items:center; justify-content:space-between; }
  .field.inline label { margin-bottom:0; }
  .switch { position:relative; width:46px; height:26px; flex:none; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; inset:0; background:#262a36; border-radius:26px;
            cursor:pointer; transition:.2s; }
  .slider::before { content:''; position:absolute; height:20px; width:20px; left:3px;
            top:3px; background:var(--text); border-radius:50%; transition:.2s; }
  .switch input:checked + .slider { background:var(--accent); }
  .switch input:checked + .slider::before { transform:translateX(20px); }
  .modal-actions { display:flex; gap:10px; align-items:center; margin-top:24px; }
  /* Transaction mapping modal */
  .modal.modal-wide { max-width:1040px; max-height:90vh; overflow-y:auto; }
  .map-cols { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }
  .map-col { display:flex; flex-direction:column; min-width:0; }
  .map-col-head { display:flex; align-items:center; gap:8px; font-size:14px;
                  font-weight:600; color:var(--text); margin-bottom:10px; }
  .map-col-head .dot { width:10px; height:10px; border-radius:50%; flex:none; }
  .dot.spend { background:#ff5fa2; } .dot.save { background:#39d3bb; } .dot.income { background:#7c5cff; }
  /* per-column running total */
  .map-sum { display:flex; align-items:center; justify-content:space-between;
             padding:11px 15px; border-radius:12px; border:1px solid; margin-bottom:11px; }
  .map-sum .ms-count { font-size:13px; color:var(--muted); }
  .map-sum .ms-total { font-size:21px; font-weight:700; letter-spacing:-.01em; }
  .map-sum.spend { background:rgba(255,95,162,.07); border-color:rgba(255,95,162,.30); }
  .map-sum.spend .ms-total { color:#ff5fa2; }
  .map-sum.save { background:rgba(57,211,187,.07); border-color:rgba(57,211,187,.30); }
  .map-sum.save .ms-total { color:#39d3bb; }
  .map-sum.income { background:rgba(124,92,255,.08); border-color:rgba(124,92,255,.32); }
  .map-sum.income .ms-total { color:#7c5cff; }
  .map-drop { flex:1; min-height:120px; max-height:58vh; overflow-y:auto; overflow-x:hidden;
              border-radius:12px; padding:3px; display:flex; flex-direction:column;
              gap:9px; transition:.12s; }
  .map-drop.over { background:#181c28; box-shadow:inset 0 0 0 1.5px var(--accent); }
  .map-card { display:flex; align-items:center; gap:11px; background:var(--card);
              border:1px solid #2b2f3d; border-left:3px solid #4a5061; border-radius:11px;
              padding:11px 13px; cursor:grab; user-select:none; transition:.1s; }
  .map-card:hover { border-color:#3a3f52; }
  .map-card:active { cursor:grabbing; }
  .map-card.dragging { opacity:.4; }
  .map-drop[data-bucket=spending] .map-card { border-left-color:#ff5fa2; }
  .map-drop[data-bucket=savings] .map-card { border-left-color:#39d3bb; }
  .map-drop[data-bucket=income] .map-card { border-left-color:#7c5cff; }
  .mc-grip { flex:none; color:#4a5061; display:flex; }
  .mc-body { min-width:0; flex:1; }
  .map-card .mc-label { font-size:13.5px; color:var(--text); font-weight:600;
              line-height:1.3; display:-webkit-box; -webkit-line-clamp:2;
              -webkit-box-orient:vertical; overflow:hidden; }
  .map-card .mc-meta { font-size:11.5px; color:var(--muted); margin-top:2px; }
  @media (max-width:720px){ .map-cols { grid-template-columns:1fr; } }
  .btn { border-radius:9px; padding:9px 18px; font-size:14px; font-weight:600;
         cursor:pointer; border:1px solid #262a36; }
  .btn-secondary { background:transparent; color:var(--muted); }
  .btn-secondary:hover { color:var(--text); }
  .btn-primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  .btn[disabled] { opacity:0.45; cursor:default; }
  .btn-reset { background:transparent; color:var(--muted); border:none; font-size:12px;
               cursor:pointer; text-decoration:underline; margin-right:auto; }
  .btn-reset:hover { color:var(--text); }
  /* Net worth card (synced from Empower via the local bridge server) */
  .nw-card { background:linear-gradient(135deg,#1c1f2b,#191b26); border:1px solid #2a2f40;
             border-radius:14px; padding:18px 22px; margin-bottom:24px; display:flex;
             align-items:center; justify-content:space-between; gap:16px; flex-wrap:wrap; }
  .nw-left { min-width:0; }
  .nw-lbl { color:var(--muted); font-size:13px; margin-bottom:4px; }
  .nw-val { font-size:30px; font-weight:700; letter-spacing:0.2px; }
  .nw-meta { color:var(--muted); font-size:12px; margin-top:5px; }
  .nw-meta.stale { color:#e0b341; }
  .nw-meta code { background:#0f1117; border:1px solid #262a36; border-radius:5px;
                  padding:1px 6px; font-size:11px; }
  .nw-actions { flex:none; }
  .nw-btn { background:var(--accent); color:#fff; border:1px solid var(--accent);
            border-radius:9px; padding:10px 18px; font-size:14px; font-weight:600;
            cursor:pointer; }
  .nw-btn[disabled] { opacity:0.6; cursor:default; }
  .nw-refresh { background:transparent; color:var(--muted); border:none; font-size:12px;
                text-decoration:underline; cursor:pointer; }
  .nw-refresh:hover { color:var(--text); }
  /* Net worth component bar (Cash / Investments / Credit Cards) */
  .nw-bar-wrap { flex:1 1 260px; min-width:220px; }
  .nw-bar { display:flex; height:40px; border-radius:8px; overflow:hidden; }
  .nw-seg { flex-shrink:1; flex-basis:0; min-width:max-content; display:flex;
            align-items:center; justify-content:center; color:#fff; font-weight:600;
            font-size:13px; white-space:nowrap; padding:0 10px; overflow:hidden; }
  .nw-seg.teal { background:#39d3bb; }
  .nw-seg.purple { background:#7c5cff; }
  .nw-seg.pink { background:#ff5fa2; }
  .nw-legend { display:flex; gap:16px; margin-top:8px; font-size:12px;
               color:var(--muted); flex-wrap:wrap; }
  .nw-legend .nw-lg { display:inline-flex; align-items:center; gap:6px; }
  .nw-legend i { display:inline-block; width:11px; height:11px; border-radius:3px; }
  .nw-legend i.teal { background:#39d3bb; }
  .nw-legend i.purple { background:#7c5cff; }
  .nw-legend i.pink { background:#ff5fa2; }
  /* Retirement calculator tab */
  .ret-inputs { background:var(--card); border:1px solid #262a36; border-radius:14px;
                padding:20px 22px; margin-bottom:8px; }
  .ret-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
              gap:14px 16px; }
  .ret-field label { display:block; font-size:12px; color:var(--muted); margin-bottom:5px; }
  .ret-field input, .ret-field select, .ret-lump-row input, .ret-lump-row select {
    background:var(--bg); border:1px solid #262a36; border-radius:8px; color:var(--text);
    padding:9px 11px; font-size:14px; }
  .ret-field input { width:100%; }
  .ret-field input:focus, .ret-field select:focus,
  .ret-lump-row input:focus, .ret-lump-row select:focus { outline:none; border-color:var(--accent); }
  .ret-pfx { position:relative; }
  .ret-pfx > span { position:absolute; left:11px; top:50%; transform:translateY(-50%);
                    color:var(--muted); font-size:14px; }
  .ret-pfx input { padding-left:24px; }
  .ret-row { margin-top:16px; }
  .ret-check { display:inline-flex; align-items:center; gap:8px; font-size:13px;
               color:var(--text); cursor:pointer; }
  .ret-accum { display:flex; gap:16px; margin-top:12px; flex-wrap:wrap; }
  .ret-accum .ret-field { min-width:180px; }
  .ret-lumps { margin-top:18px; border-top:1px solid #262a36; padding-top:14px; }
  .ret-lumps-hd { display:flex; align-items:center; justify-content:space-between;
                  font-size:13px; color:var(--muted); margin-bottom:8px; }
  .ret-lump-row { display:flex; align-items:center; gap:8px; margin-bottom:8px;
                  flex-wrap:wrap; font-size:13px; color:var(--muted); }
  .ret-lump-row .lump-amt { width:120px; }
  .ret-defaults { margin-top:18px; border-top:1px solid #262a36; padding-top:14px;
                  display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .ret-defaults-lbl { font-size:13px; color:var(--muted); }
  .ret-year-btns { display:inline-flex; gap:8px; flex-wrap:wrap; }
  .ret-year-btn { padding:5px 13px; font-size:13px; font-weight:600; border-radius:8px;
                  background:var(--bg); border:1px solid #33384a; color:var(--text);
                  cursor:pointer; transition:border-color .15s, color .15s; }
  .ret-year-btn:hover { border-color:var(--accent); color:var(--accent); }
  .ret-chart-card { position:relative; }
  .ret-legend { position:absolute; top:14px; right:16px; z-index:2; display:flex;
                gap:14px; font-size:12px; color:var(--muted);
                background:rgba(15,17,23,0.65); padding:6px 11px; border-radius:8px;
                border:1px solid #262a36; }
  .ret-legend span { display:inline-flex; align-items:center; gap:6px; }
  .ret-legend i { width:16px; height:3px; border-radius:2px; display:inline-block; }
  .ret-legend i.lav { background:#a894ff; }
  .ret-legend i.teal { background:#39d3bb; height:4px; }
  .ret-legend i.pink { background:#ff5fa2; }
</style>
</head>
<body>
<button class="power-btn" id="powerBtn" title="Shut down &amp; close" aria-label="Shut down and close">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="3" x2="12" y2="12"/><path d="M6.4 6.4a9 9 0 1 0 11.2 0"/></svg></button>
<button class="reset-btn" id="resetBtn" title="Reset — clear all data" aria-label="Reset all data">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg></button>
<button class="gear-btn import-btn" id="importBtn" title="Load new data" aria-label="Load new data">&#11014;</button>
<button class="gear-btn" id="gearBtn" title="Settings" aria-label="Settings">&#9881;</button>
<div class="modal-overlay" id="importOverlay">
  <div class="modal">
    <h2>Load transactions</h2>
    <p class="msub">Drop your account export or paste the rows. This replaces the current data and rebuilds the dashboard.</p>
    <div class="drop" id="importDrop"><span id="importDropText">Drop CSV / Excel export here, or click to choose</span></div>
    <input type="file" id="importFile" accept=".csv,.tsv,.txt" hidden>
    <textarea id="importPaste" rows="5" placeholder="…or paste rows here (Date  Time  Amount  Type  Description)"></textarea>
    <div class="nw-meta" id="importMsg" style="margin-top:8px"></div>
    <div class="modal-actions">
      <span style="flex:1"></span>
      <button class="btn btn-secondary" id="importCancel">Cancel</button>
      <button class="btn btn-primary" id="importGo">Load</button>
    </div>
  </div>
</div>
<div class="modal-overlay" id="settingsOverlay">
  <div class="modal">
    <h2>Settings</h2>
    <p class="msub">Override the default assumptions used in the projections.</p>
    <div class="field">
      <label for="setRent">Monthly housing</label>
      <div class="input-prefix"><span>$</span>
        <input type="number" id="setRent" step="0.01" min="0"></div>
    </div>
    <div class="field">
      <label for="setBiweek">Spending Account Deposits</label>
      <div class="input-prefix"><span>$</span>
        <input type="number" id="setBiweek" step="1" min="0"></div>
      <div class="hint">Direct deposit into spending account per pay period.</div>
    </div>
    <div class="field">
      <label for="setK401">Annual 401k contribution</label>
      <div class="input-prefix"><span>$</span>
        <input type="number" id="setK401" step="1" min="0"></div>
    </div>
    <div class="field inline">
      <label for="setBonus">Receive bonuses?</label>
      <label class="switch"><input type="checkbox" id="setBonus"><span class="slider"></span></label>
    </div>
    <div class="field" id="bonusThresholdField">
      <label for="setBonusThreshold">Bonus trigger threshold</label>
      <div class="input-prefix"><span>$</span>
        <input type="number" id="setBonusThreshold" step="100" min="0"></div>
      <div class="hint">Paychecks above this are treated as one-time bonuses.</div>
    </div>
    <div class="section-label">Transaction mapping</div>
    <p class="msub">Sort your transaction groups into Spending, Savings &amp; Investing, or Income.</p>
    <div class="field">
      <button class="btn btn-primary" id="editMapping" type="button">Edit Tx Mapping</button>
    </div>
    <div class="modal-actions">
      <button class="btn-reset" id="resetAll">Reset &amp; start over</button>
      <button class="btn btn-secondary" id="cancelSettings">Cancel</button>
      <button class="btn btn-primary" id="saveSettings">Save</button>
    </div>
  </div>
</div>
<div class="modal-overlay" id="onboardingOverlay">
  <div class="modal">
    <h2>Welcome &#128075;</h2>
    <p class="msub">A few quick questions to tailor your savings &amp; spending
      projections. Your answers stay on this machine.</p>
    <div class="field">
      <label for="obRent">What is your monthly housing expense?</label>
      <div class="input-prefix"><span>$</span>
        <input type="number" id="obRent" step="0.01" min="0" placeholder="0.00"></div>
    </div>
    <div class="field inline">
      <label for="obHasOther">Any additional accounts where income is received?</label>
      <label class="switch"><input type="checkbox" id="obHasOther"><span class="slider"></span></label>
    </div>
    <div class="field" id="obBiweekField" style="display:none">
      <label for="obBiweek">Total bi-weekly amount deposited into those accounts?</label>
      <div class="input-prefix"><span>$</span>
        <input type="number" id="obBiweek" step="1" min="0" placeholder="0"></div>
    </div>
    <div class="field">
      <label for="obK401">What is your annual 401k contribution?</label>
      <div class="input-prefix"><span>$</span>
        <input type="number" id="obK401" step="1" min="0" placeholder="0"></div>
    </div>
    <div class="field inline">
      <label for="obBonus">Do you receive bonuses?</label>
      <label class="switch"><input type="checkbox" id="obBonus"><span class="slider"></span></label>
    </div>
    <div class="field" id="obThresholdField" style="display:none">
      <label for="obThreshold">Above what amount is income an ad-hoc bonus?</label>
      <div class="input-prefix"><span>$</span>
        <input type="number" id="obThreshold" step="100" min="0" placeholder="0"></div>
    </div>
    <div class="modal-actions">
      <button class="btn btn-primary" id="finishOnboarding" style="margin-left:auto">Get started</button>
    </div>
  </div>
</div>
<div class="modal-overlay" id="mappingOverlay">
  <div class="modal modal-wide">
    <h2>Sort your transactions</h2>
    <p class="msub">Drag each group into how it should count toward your numbers. We've pre-sorted the ones we recognize — adjust any you disagree with, then click Done.</p>
    <div class="map-cols">
      <div class="map-col">
        <div class="map-col-head"><span class="dot spend"></span>Spending</div>
        <div class="map-sum spend" id="sum-spending"><span class="ms-count">0 txns</span><span class="ms-total">$0</span></div>
        <div class="map-drop" data-bucket="spending" id="col-spending"></div>
      </div>
      <div class="map-col">
        <div class="map-col-head"><span class="dot save"></span>Savings &amp; Investing</div>
        <div class="map-sum save" id="sum-savings"><span class="ms-count">0 txns</span><span class="ms-total">$0</span></div>
        <div class="map-drop" data-bucket="savings" id="col-savings"></div>
      </div>
      <div class="map-col">
        <div class="map-col-head"><span class="dot income"></span>Income</div>
        <div class="map-sum income" id="sum-income"><span class="ms-count">0 txns</span><span class="ms-total">$0</span></div>
        <div class="map-drop" data-bucket="income" id="col-income"></div>
      </div>
    </div>
    <div class="modal-actions">
      <button class="btn btn-secondary" id="cancelMapping">Cancel</button>
      <button class="btn btn-primary" id="doneMapping" style="margin-left:auto">Done</button>
    </div>
  </div>
</div>
<div class="wrap">
  <div class="title-row">
    <img class="logo" src="/dolphin.png" alt="" onerror="this.remove()">
    <h1 id="mainTitle"></h1>
  </div>
  <p class="sub" id="sub"></p>
  <div class="nw-card" id="nwCard">
    <div class="nw-left">
      <div class="nw-lbl">Net worth <span style="opacity:.7">· Empower</span></div>
      <div class="nw-val" id="nwVal">—</div>
      <div class="nw-meta" id="nwMeta">Loading…</div>
    </div>
    <div class="nw-bar-wrap" id="nwBarWrap"></div>
    <div class="nw-actions" id="nwActions"></div>
  </div>
  <div class="tabs" id="tabs"></div>
  <div class="subtabs" id="subtabs"></div>
  <div id="panels"></div>
</div>

<script>
// Visiting <url>?reset wipes this site's localStorage (settings + retirement
// inputs) for a clean first-run, then reloads to the plain URL.
if (location.search.indexOf('reset') !== -1) {
  try { localStorage.clear(); } catch (e) {}
  location.replace(location.pathname);
}
const PANELS_PY = %%PANELS_JSON%%;   // Python-rendered panels (authoritative default)
const PRIMITIVES = %%PRIMITIVES_JSON%%;
const MARKET_RAW = %%MARKET_DATA_JSON%%;
const MARKET = (MARKET_RAW && MARKET_RAW.data) || [];   // [ [year, stock, bond, inflation], ... ]
let PANELS = PANELS_PY;              // current panels being rendered
const fmt = n => (n<0?'-':'') + '$' + Math.abs(Math.round(n)).toLocaleString();
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const r2 = v => Math.round(v*100)/100;
const money2 = v => { if (Math.abs(v)<0.005) v=0;
  return '$'+v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); };
const money0 = v => { if (Math.abs(v)<0.5) v=0;
  return '$'+Math.round(v).toLocaleString('en-US'); };

// ── Client-side recompute ────────────────────────────────────────────────────
// Faithful JS port of compute()/compute_monthly()/build_panel() driven by the
// embedded per-panel primitives, so the four settings knobs recompute the cards,
// tables and charts live. The venmo/gambling/TD conventions are fixed (baked
// into the primitives); only rent, biweekly deposit, 401k and bonus threshold
// vary here.
function computePanels(settings) {
  return PRIMITIVES.map(p => computePanel(p, settings));
}

function computePanel(prim, settings) {
  const rent = prim.rent_locked ? prim.rent_default : settings.rent;
  const biweekly = settings.biweekly_deposit;
  const k401_annual = settings.k401_annual;
  const thr = settings.receive_bonuses ? settings.bonus_threshold : Infinity;
  const year = prim.year;
  const asof = new Date(prim.as_of[0], prim.as_of[1]-1, prim.as_of[2]);
  const yearEnd = new Date(year, 11, 31);
  const months_ytd = prim.months_ytd, months_remaining = prim.months_remaining;
  const per_pay = k401_annual / 26;

  // Payroll: regular vs bonus by threshold.
  const regular = prim.payroll.filter(p => p.amt < thr);
  const n_regular = regular.length;
  const bonus = prim.payroll.filter(p => p.amt >= thr).reduce((a,p)=>a+p.amt, 0);
  const pay = Array(12).fill(0);
  regular.forEach(p => { pay[p.m-1]++; });

  // Savings (excl is knob-independent; 401k scales with pay counts + annual).
  const excl = prim.excl.slice();
  const savings_excl_401k = r2(excl.reduce((a,b)=>a+b, 0));
  const k401_ytd = r2((n_regular/26) * k401_annual);
  const savings_incl_401k = r2(savings_excl_401k + k401_ytd);
  const k401m = pay.map(c => r2(c*per_pay));
  const inclm = excl.map((e,i) => r2(e + k401m[i]));

  // Spending: spending-account net (+ prepaid-rent shift per month) + biweekly + fixed rest.
  const spendMainNet = r2(prim.spendMain.reduce((a,t)=>a+t.amt, 0));
  const s5 = Array(12).fill(0);
  prim.spendMain.forEach(t => {
    const m = t.m;
    const nxt = m < 12 ? new Date(year, m, 1) : null;   // month m+1, day 1
    if (rent>0 && t.amt>=rent && t.day>=21 && nxt && nxt<=asof) {
      s5[m]   += rent;          // index m == month m+1 (0-based)
      s5[m-1] += t.amt - rent;
    } else {
      s5[m-1] += t.amt;
    }
  });
  const biweek_ytd = r2(biweekly * n_regular);
  const actual_fixed_sum = r2(prim.actual_fixed.reduce((a,b)=>a+b, 0));
  const spending = r2(spendMainNet + biweek_ytd + actual_fixed_sum);
  const actualm = Array.from({length:12}, (_,i) =>
    r2(s5[i] + biweekly*pay[i] + prim.actual_fixed[i]));
  const expectedm = pay.map(c => r2(biweekly*c));

  // Assumed January rent (prepaid prior Dec, out of window).
  const jan_has_own_rent = prim.spendMain.some(
    t => t.m===1 && t.day<21 && t.amt>=rent);
  const assumed_rent = (rent>0 && !jan_has_own_rent) ? rent : 0;

  // Projection (flat run-rate, plus seasonal lift when available).
  const sav_recurring = savings_excl_401k - bonus - prim.td_net;
  const sav_monthly = months_ytd ? sav_recurring/months_ytd : 0;
  const proj_sav_excl_flat = savings_excl_401k + sav_monthly*months_remaining;
  const sp_monthly = months_ytd ? spending/months_ytd : 0;
  const proj_spending_flat = spending + sp_monthly*months_remaining;
  let proj_spending_seasonal = proj_spending_flat;
  if (rent>0 && prim.has_seasonality) {
    const factor = prim.seasonal_factor;
    const ytd_nonrent = spending - rent*months_ytd;
    const ytd_nonrent_monthly = months_ytd ? ytd_nonrent/months_ytd : 0;
    const jundec_rem = ytd_nonrent_monthly*factor*prim.mo_jd_remain;
    const janmay_rem = ytd_nonrent_monthly*prim.mo_jm_remain;
    const rent_remaining = rent*months_remaining;
    proj_spending_seasonal = spending + jundec_rem + janmay_rem + rent_remaining;
  }
  const proj_spending = proj_spending_seasonal;
  const lift = proj_spending_seasonal - proj_spending_flat;
  const proj_sav_excl = proj_sav_excl_flat - lift;
  const proj_sav_incl = proj_sav_excl + k401_annual;

  // Projected future paychecks: continue the 14-day cadence past the last one.
  const payroll_dates = regular.map(p => new Date(year, p.m-1, p.day))
                               .sort((a,b) => a-b);
  const proj_pay = Array(12).fill(0);
  if (payroll_dates.length) {
    const d = new Date(payroll_dates[payroll_dates.length-1]);
    d.setDate(d.getDate()+14);
    while (d.getTime() <= yearEnd.getTime()) {
      if (d.getTime() > asof.getTime()) proj_pay[d.getMonth()]++;
      d.setDate(d.getDate()+14);
    }
  }

  const monthly = { excl, k401: k401m, incl: inclm, actual: actualm,
    expected: expectedm, paychecks: pay, proj_paychecks: proj_pay,
    last_month: prim.last_month };
  const res = { proj_savings_incl_401k: proj_sav_incl, savings_excl_401k,
    k401_ytd, proj_spending, spending, savings_incl_401k, assumed_rent };

  return buildPanelJS(res, monthly, biweekly, rent, k401_annual, prim);
}

function buildPanelJS(res, monthly, biweekly, rent, k401_annual, prim) {
  const year = prim.year, historical = prim.historical;
  const asof = new Date(prim.as_of[0], prim.as_of[1]-1, prim.as_of[2]);
  const asofMonth = prim.as_of[1], asofDay = prim.as_of[2];
  const last = monthly.last_month;
  const proj_last = (last && last < 12) ? 12 : last;
  const n_proj = proj_last - last;
  const excl_proj = monthly.excl.slice();
  const k401_proj = monthly.k401.slice();
  const actual_proj = monthly.actual.slice();
  const expected_proj = monthly.expected.slice();
  const paychecks_proj = monthly.paychecks.slice();
  const proj_pay = monthly.proj_paychecks;
  const excl_rem = Array(12).fill(0), k401_rem = Array(12).fill(0);
  const rent_rem = Array(12).fill(0), other_rem = Array(12).fill(0);
  const cur = last - 1;
  let cur_frac = 0;
  if (last && !historical && last === asofMonth) {
    const days_in_month = new Date(year, last, 0).getDate();
    const days_left = days_in_month - asofDay;
    if (days_left > 0 && asof < new Date(year, 11, 31))
      cur_frac = days_left / days_in_month;
  }
  if (last && !historical && (n_proj > 0 || cur_frac > 0)) {
    const cur_pays = cur_frac > 0 ? proj_pay[cur] : 0;
    let future_pays = 0;
    for (let i = last; i < proj_last; i++) future_pays += proj_pay[i];
    const all_pays = (cur_pays + future_pays) || (n_proj + cur_frac) || 1;
    const month_units = n_proj + cur_frac;
    const proj_excl = res.proj_savings_incl_401k - k401_annual;
    const rem_excl = proj_excl - res.savings_excl_401k;
    const rem_k401 = k401_annual - res.k401_ytd;
    const rem_spend = res.proj_spending - res.spending;
    const rem_rent = rent * n_proj;
    const rem_budget = biweekly * all_pays;
    const rem_other = rem_spend - rem_rent - rem_budget;
    if (cur_frac > 0) {
      const wt = all_pays ? cur_pays/all_pays : 0;
      excl_rem[cur] = r2(rem_excl * wt);
      k401_rem[cur] = r2(rem_k401 * wt);
      rent_rem[cur] = 0;
      other_rem[cur] = r2(biweekly*cur_pays
        + (month_units ? rem_other*cur_frac/month_units : 0));
      paychecks_proj[cur] = monthly.paychecks[cur] + cur_pays;
      expected_proj[cur] = r2(biweekly * paychecks_proj[cur]);
      if (excl_proj[cur] < 0) {
        excl_rem[cur] = r2(excl_rem[cur] + excl_proj[cur]);
        excl_proj[cur] = 0;
      }
    }
    for (let m = last+1; m <= proj_last; m++) {
      const pays = proj_pay[m-1];
      const wt = all_pays ? pays/all_pays : 1/month_units;
      excl_proj[m-1] = r2(rem_excl * wt);
      k401_proj[m-1] = r2(rem_k401 * wt);
      actual_proj[m-1] = r2(rent + biweekly*pays + rem_other/month_units);
      expected_proj[m-1] = r2(biweekly * pays);
      paychecks_proj[m-1] = proj_pay[m-1];
    }
  }
  let assumed_rent_month = -1;
  if (res.assumed_rent) {
    actual_proj[0] = r2(actual_proj[0] + res.assumed_rent);
    assumed_rent_month = 0;
  }

  const data = {
    labels: MONTHS, excl: monthly.excl, k401: monthly.k401, incl: monthly.incl,
    excl_proj, k401_proj, excl_rem, k401_rem,
    total_excl: r2(res.savings_excl_401k), total_incl: r2(res.savings_incl_401k),
    last_month: last, proj_last, cur_partial: cur_frac > 0 ? cur : -1,
    last_month_label: last ? MONTHS[last-1] : "",
  };
  const spend = {
    actual: monthly.actual, expected: monthly.expected,
    paychecks: monthly.paychecks, actual_proj, expected_proj,
    paychecks_proj, rent_rem, other_rem, assumed_rent_month,
  };
  return {
    year, label: prim.label || String(year), historical,
    ytd_heading: `YTD (Jan 1 – ${MONTHS[asofMonth-1]} ${asofDay}, ${year})`,
    proj_heading: `Projected to Dec 31, ${year}`,
    sav_excl: money2(res.savings_excl_401k), sav_incl: money2(res.savings_incl_401k),
    spending: money2(res.spending + res.assumed_rent),
    proj_sav: "~" + money0(res.proj_savings_incl_401k),
    proj_spend: "~" + money0(res.proj_spending),
    biweek: String(Math.round(biweekly)),
    rent, data, spend,
  };
}

const chartInstances = [];
const tabsEl = document.getElementById('tabs');
const subtabsEl = document.getElementById('subtabs');
const panelsEl = document.getElementById('panels');

function panelHTML(P, idx) {
  // Completed years carry every month as actual, so the projection cards are
  // omitted (they'd just restate the YTD totals).
  const projSection = P.historical ? '' : `
  <h2>${P.proj_heading}</h2>
  <div class="cards">
    <div class="card"><div class="lbl">Projected savings by 12/31 (incl 401k)</div>
      <div class="val proj">${P.proj_sav}</div></div>
    <div class="card"><div class="lbl">Projected post-tax spending by 12/31</div>
      <div class="val proj">${P.proj_spend}</div></div>
  </div>`;
  return `
  <h2>${P.ytd_heading}</h2>
  <div class="cards">
    <div class="card"><div class="lbl">Savings to date (excl 401k)</div>
      <div class="val">${P.sav_excl}</div></div>
    <div class="card"><div class="lbl">Savings to date (incl 401k)</div>
      <div class="val">${P.sav_incl}</div></div>
    <div class="card"><div class="lbl">Post-tax spending to date</div>
      <div class="val">${P.spending}</div></div>
  </div>${projSection}
  <h2>Monthly Savings breakdown</h2>
  <div class="chart-card"><canvas id="chart-${idx}" height="140"></canvas></div>
  <table>
    <thead><tr><th>Month</th><th>Saved (excl 401k)</th><th>401k</th><th>Saved (incl 401k)</th></tr></thead>
    <tbody id="tbody-${idx}"></tbody>
    <tfoot><tr><td>Total</td><td id="fExcl-${idx}"></td><td id="fK-${idx}"></td><td id="fIncl-${idx}"></td></tr></tfoot>
  </table>
  <p class="foot" id="note-${idx}"></p>
  <h2>Spending vs. $${P.biweek} / pay-period budget</h2>
  <div class="chart-card">
    <div class="spend-legend">
      <span class="lg-item"><span class="lg-line"><span class="lg-dot"></span></span>Budget (housing + $${P.biweek} &times; pay periods)</span>
      <span class="lg-item"><span class="lg-box lg-teal"></span>Housing</span>
      <span class="lg-item"><span class="lg-box lg-purple"></span>Other spending</span>
    </div>
    <canvas id="spendChart-${idx}" height="140"></canvas>
  </div>
  <table>
    <thead><tr><th>Month</th><th>Housing</th><th>$${P.biweek} biweekly spending</th><th>Over / Under</th><th>Total</th></tr></thead>
    <tbody id="spendTbody-${idx}"></tbody>
    <tfoot><tr><td>Total</td><td id="sfRent-${idx}"></td><td id="sfBiweek-${idx}"></td><td id="sfOver-${idx}"></td><td id="sfTotal-${idx}"></td></tr></tfoot>
  </table>
  <p class="foot" id="spendNote-${idx}"></p>`;
}

function initPanel(P, idx) {
  const DATA = P.data, SPEND = P.spend, RENT = P.rent, BIWEEK = P.biweek;

  // Monthly Savings breakdown table. Hovering a month row highlights it (purple)
  // and turns the Total row teal, with the Total showing cumulative sums from
  // January through the hovered month. Mousing out restores the full-year Total.
  const tb = document.getElementById('tbody-'+idx);
  const fExcl = document.getElementById('fExcl-'+idx);
  const fK = document.getElementById('fK-'+idx);
  const fIncl = document.getElementById('fIncl-'+idx);
  const footRow = fExcl.closest('tr');
  const fLabel = footRow.firstElementChild;

  const totExcl = DATA.total_excl;
  const totK = DATA.k401.reduce((a,b)=>a+b,0);
  const totIncl = DATA.total_incl;
  const rowEls = [];
  const setFoot = (label, e, k, inc) => {
    fLabel.textContent = label;
    fExcl.textContent = fmt(e); fK.textContent = fmt(k); fIncl.textContent = fmt(inc);
  };
  const resetFoot = () => {
    footRow.classList.remove('foot-hover');
    rowEls.forEach(r => r.classList.remove('row-hover'));
    setFoot('Total', totExcl, totK, totIncl);
  };

  DATA.labels.forEach((lbl,i) => {
    if (i+1 > DATA.last_month) return; // only show months with data
    const tr = document.createElement('tr');
    // Saved is never shown below zero: a net drawdown month (e.g. an in-progress
    // month with outflows but no paycheck yet) displays as $0, matching the
    // floored chart bar. Totals/cumulative still use the real underlying values.
    const exclD = Math.max(0, DATA.excl[i]);
    const inclD = Math.max(0, DATA.incl[i]);
    tr.innerHTML = `<td>${lbl}</td>`
      + `<td class="pos">${fmt(exclD)}</td>`
      + `<td>${fmt(DATA.k401[i])}</td>`
      + `<td>${fmt(inclD)}</td>`;
    tr.addEventListener('mouseenter', () => {
      rowEls.forEach(r => r.classList.remove('row-hover'));
      tr.classList.add('row-hover');
      let ce=0, ck=0, ci=0;
      for (let j=0; j<=i; j++) { ce+=DATA.excl[j]; ck+=DATA.k401[j]; ci+=DATA.incl[j]; }
      footRow.classList.add('foot-hover');
      setFoot('Total · Jan–'+lbl, ce, ck, ci);
    });
    tb.appendChild(tr);
    rowEls.push(tr);
  });
  tb.addEventListener('mouseleave', resetFoot);
  resetFoot();

  // Solid = actual months; translucent = projected future months (same hue).
  const SOLID_PURPLE = '#7c5cff', TRANS_PURPLE = 'rgba(124,92,255,0.32)';
  const SOLID_TEAL   = '#39d3bb', TRANS_TEAL   = 'rgba(57,211,187,0.32)';
  const isProj = i => i >= DATA.last_month;  // 0-based index; months > last_month

  const projLast = DATA.proj_last;
  const shown = DATA.labels.slice(0, projLast);
  const exclData = DATA.excl_proj.slice(0, projLast);
  const k401Data = DATA.k401_proj.slice(0, projLast);
  // Mid-month caps: actual-so-far stays solid; the projected remainder of the
  // current month rides on top translucent (same hue).
  const exclRem = (DATA.excl_rem||[]).slice(0, projLast);
  const k401Rem = (DATA.k401_rem||[]).slice(0, projLast);
  const legendNoRem = {color:'#e6e8ee', filter:it=>!/ remainder$/.test(it.text)};
  chartInstances.push(new Chart(document.getElementById('chart-'+idx), {
    type:'bar',
    data:{
      labels: shown,
      datasets:[
        {label:'Saved (excl 401k)', data:exclData,
         backgroundColor:exclData.map((_,i)=>isProj(i)?TRANS_PURPLE:SOLID_PURPLE),
         borderRadius:6, stack:'s'},
        {label:'Saved (excl 401k) · projected remainder', data:exclRem,
         backgroundColor:TRANS_PURPLE, borderRadius:6, stack:'s'},
        {label:'401k', data:k401Data,
         backgroundColor:k401Data.map((_,i)=>isProj(i)?TRANS_TEAL:SOLID_TEAL),
         borderRadius:6, stack:'s'},
        {label:'401k · projected remainder', data:k401Rem,
         backgroundColor:TRANS_TEAL, borderRadius:6, stack:'s'}
      ]
    },
    options:{
      plugins:{
        legend:{labels:legendNoRem},
        tooltip:{filter:it=>!(/ remainder$/.test(it.dataset.label) && !it.parsed.y),
          callbacks:{label:c=>c.dataset.label+': '+fmt(c.parsed.y)}}
      },
      scales:{
        x:{stacked:true, ticks:{color:'#9aa0ad'}, grid:{color:'#262a36'}},
        y:{stacked:true, ticks:{color:'#9aa0ad', callback:v=>fmt(v)},
           grid:{color:'#262a36'}}
      }
    }
  }));

  document.getElementById('note-'+idx).textContent =
    'Savings = main-account accumulation + Robinhood/Wealthfront/savings transfers '
    + '+ TD checking transfers, plus linear 401k ($' + Math.round(DATA.k401[0]||942)
    + '/regular paycheck). Same conventions as the finance projector. '
    + (projLast > DATA.last_month
        ? 'Translucent bars (' + DATA.labels[DATA.last_month] + '–'
          + DATA.labels[projLast-1] + ') are projected, not actual.'
        : '')
    + (DATA.cur_partial >= 0
        ? ' ' + DATA.labels[DATA.cur_partial] + ' is still in progress: the solid'
          + ' base is banked so far, the translucent cap is the projected rest of'
          + ' the month.'
        : '');

  // ── Spending vs. budget ──
  const sLast = DATA.proj_last;
  const sLabels = DATA.labels.slice(0, sLast);
  const sActual = SPEND.actual_proj.slice(0, sLast);
  // Budget = rent + biweekly deposit per pay period that month
  const sExpected = SPEND.expected_proj.slice(0, sLast).map(v => +(v + RENT).toFixed(2));
  // For the in-progress month the rent shown solid is only the elapsed
  // fraction; its projected remainder is carried in SPEND.rent_rem.
  const sRentRem = (SPEND.rent_rem||[]).slice(0, sLast);
  const sOtherRem = (SPEND.other_rem||[]).slice(0, sLast);
  // Solid rent is the full month except for the in-progress month, where the
  // unelapsed fraction moves into the translucent rent cap (no double-count).
  // Cap the rent portion at the month's actual total so a partial/low-spend
  // month (e.g. a stale paste that ends mid-month) can't push "other spending"
  // negative — the decomposition stays >=0 and still sums to the real total.
  const sRent = sActual.map((a,i) => {
    const full = RENT - (sRentRem[i]||0);
    return +Math.min(full, Math.max(a, 0)).toFixed(2);
  });
  const sOther = sActual.map((a,i) => +Math.max(0, a - sRent[i]).toFixed(2));

  chartInstances.push(new Chart(document.getElementById('spendChart-'+idx), {
    data:{
      labels: sLabels,
      datasets:[
        {type:'bar', label:'Housing', data:sRent, stack:'spend',
         backgroundColor:sRent.map((_,i)=>(isProj(i)||i===SPEND.assumed_rent_month)?TRANS_TEAL:SOLID_TEAL),
         borderRadius:{topLeft:0,topRight:0,bottomLeft:6,bottomRight:6}, order:3},
        {type:'bar', label:'Housing · projected remainder', data:sRentRem, stack:'spend',
         backgroundColor:TRANS_TEAL, borderRadius:0, order:3},
        {type:'bar', label:'Other spending', data:sOther, stack:'spend',
         backgroundColor:sOther.map((_,i)=>isProj(i)?TRANS_PURPLE:SOLID_PURPLE),
         borderRadius:{topLeft:6,topRight:6,bottomLeft:0,bottomRight:0}, order:3},
        {type:'bar', label:'Other spending · projected remainder', data:sOtherRem, stack:'spend',
         backgroundColor:TRANS_PURPLE,
         borderRadius:{topLeft:6,topRight:6,bottomLeft:0,bottomRight:0}, order:3},
        {type:'line', label:'Budget (housing + $'+BIWEEK+' × pay periods)', data:sExpected,
         stack:'budget',
         borderColor:'#39d3bb', backgroundColor:'#39d3bb', borderWidth:2,
         borderDash:[6,4], pointRadius:4, pointHoverRadius:5, tension:0, order:1}
      ]
    },
    options:{
      plugins:{
        legend:{display:false},
        tooltip:{filter:it=>!(/ remainder$/.test(it.dataset.label) && !it.parsed.y),
          callbacks:{
          label:c=>c.dataset.label+': '+fmt(c.parsed.y),
          afterBody:(items)=>{
            const i = items[0].dataIndex;
            const d = sActual[i] - sExpected[i];
            const pp = SPEND.paychecks_proj[i];
            return 'Total: ' + fmt(sActual[i]) + '\n'
                   + (d>=0 ? 'Over budget by ' : 'Under budget by ') + fmt(Math.abs(d))
                   + '  ('+pp+' pay period'+(pp===1?'':'s')+')';
          }
        }}
      },
      scales:{
        x:{stacked:true, ticks:{color:'#9aa0ad'}, grid:{color:'#262a36'}},
        y:{stacked:true, ticks:{color:'#9aa0ad', callback:v=>fmt(v)},
           grid:{color:'#262a36'}, beginAtZero:true}
      }
    }
  }));

  // ── Spending breakdown table (mirrors the savings table's hover behavior) ──
  // Budget = Rent + $BIWEEK biweekly spending; Over/Under = Total − Budget.
  // Hovering a month shows cumulative Jan–month totals in the footer.
  const stb = document.getElementById('spendTbody-'+idx);
  const sfRent = document.getElementById('sfRent-'+idx);
  const sfBiweek = document.getElementById('sfBiweek-'+idx);
  const sfOver = document.getElementById('sfOver-'+idx);
  const sfTotal = document.getElementById('sfTotal-'+idx);
  const sFootRow = sfRent.closest('tr');
  const sFootLabel = sFootRow.firstElementChild;

  const sMonths = DATA.last_month;             // actual months only, like the savings table
  const rentArr = [], biweekArr = [], totalArr = [], overArr = [];
  for (let i = 0; i < sMonths; i++) {
    rentArr.push(RENT);
    biweekArr.push(SPEND.expected_proj[i]);
    totalArr.push(SPEND.actual_proj[i]);
    overArr.push(SPEND.actual_proj[i] - (RENT + SPEND.expected_proj[i]));
  }
  const sSum = a => a.reduce((x,y) => x+y, 0);
  const overStr = v => (v >= 0 ? '+' : '') + fmt(v);   // + = over budget, − = under
  const sRows = [];
  const setSFoot = (label, n) => {
    sFootLabel.textContent = label;
    sfRent.textContent = fmt(sSum(rentArr.slice(0,n)));
    sfBiweek.textContent = fmt(sSum(biweekArr.slice(0,n)));
    sfOver.textContent = overStr(sSum(overArr.slice(0,n)));
    sfTotal.textContent = fmt(sSum(totalArr.slice(0,n)));
  };
  const resetSFoot = () => {
    sFootRow.classList.remove('foot-hover');
    sRows.forEach(r => r.classList.remove('row-hover'));
    setSFoot('Total', sMonths);
  };
  for (let i = 0; i < sMonths; i++) {
    const tr = document.createElement('tr');
    const oCls = overArr[i] > 0 ? 'neg' : 'pos';   // over budget red, under budget teal
    tr.innerHTML = `<td>${DATA.labels[i]}</td>`
      + `<td>${fmt(rentArr[i])}</td>`
      + `<td>${fmt(biweekArr[i])}</td>`
      + `<td class="${oCls}">${overStr(overArr[i])}</td>`
      + `<td>${fmt(totalArr[i])}</td>`;
    tr.addEventListener('mouseenter', () => {
      sRows.forEach(r => r.classList.remove('row-hover'));
      tr.classList.add('row-hover');
      sFootRow.classList.add('foot-hover');
      setSFoot('Total · Jan–'+DATA.labels[i], i+1);
    });
    stb.appendChild(tr);
    sRows.push(tr);
  }
  stb.addEventListener('mouseleave', resetSFoot);
  resetSFoot();

  const aActual = sActual.slice(0, DATA.last_month);
  const aExpected = sExpected.slice(0, DATA.last_month);
  const sOver = aActual.reduce((a,b)=>a+b,0) - aExpected.reduce((a,b)=>a+b,0);
  const overWord = sOver >= 0 ? 'above' : 'below';
  document.getElementById('spendNote-'+idx).textContent =
    'Each bar = total actual post-tax spending, split into housing ($'
    + RENT.toLocaleString() + '/mo, teal) and everything else (purple). '
    + 'Dashed line = budget = housing + $'+BIWEEK+' per pay period that month. '
    + 'Net spending ran ' + fmt(Math.abs(sOver)) + ' ' + overWord + ' budget over the shown period.'
    + (sLast > DATA.last_month
        ? ' Translucent bars (' + DATA.labels[DATA.last_month] + '–'
          + DATA.labels[sLast-1] + ') are projected spending, not actual.'
        : '')
    + (DATA.cur_partial >= 0
        ? ' ' + DATA.labels[DATA.cur_partial] + ' is mid-month: solid = spent so'
          + ' far, translucent cap = projected rest of the month.'
        : '')
    + (SPEND.assumed_rent_month >= 0
        ? ' ' + DATA.labels[SPEND.assumed_rent_month] + '’s housing (translucent'
          + ' teal) is assumed paid in full — it was prepaid the prior'
          + ' December, outside this year’s data.'
        : '');
}

// ── Retirement calculator (FIRECalc-style historical cycles) ──────────────────
// Runs your plan through every historical N-year window in MARKET (annual S&P
// total return, 10yr Treasury total return, CPI) and reports the success rate +
// ending-balance spread. Simulated in real (today's) dollars: constant spending
// keeps purchasing power; returns are deflated by CPI. Supports pre-retirement
// accumulation (contributions until a future retirement year) and lump-sum
// add/withdraws. Timing: start-of-year cashflows, then that year's return,
// rebalanced to target allocation; a cycle fails if the portfolio hits <= 0.
function simulate(cfg) {
  const M = MARKET, n = M.length;
  if (!n) return null;
  const accum = cfg.retiredNow ? 0 : Math.max(0, cfg.retireYear - cfg.thisYear);
  const total = cfg.years;            // total horizon your money must last (FIRECalc semantics)
  const retire = total - accum;       // withdrawal years = total minus accumulation
  if (total < 1 || total > n) return { cycles: 0, tooLong: total > n, total, accum };
  if (retire < 1) return { cycles: 0, badAccum: true, total, accum };
  const trajectories = [];
  let success = 0;
  for (let s = 0; s + total <= n; s++) {
    let p = cfg.portfolio, cum = 1, alive = true, endingActual = 0;
    const path = [p];
    for (let t = 0; t < total; t++) {
      const row = M[s + t], stock = row[1], bond = row[2], infl = row[3];
      const cal = cfg.thisYear + t;
      for (const L of cfg.lumps) {
        if (L.year === cal) { const amt = L.inflAdj ? L.amount : L.amount / cum; p += L.add ? amt : -amt; }
      }
      if (t < accum) p += cfg.contribution; else p -= cfg.spending;
      if (p <= 0) {
        // Depleted. Report the shortfall depth (FIRECalc shows this as a negative
        // "low"): keep drawing inflation-constant spending for the remaining years
        // but apply no market return to a depleted account. Success / average /
        // median / high still treat a failed cycle as $0 — which is the basis on
        // which FIRECalc's *average* is reported, so those stay aligned.
        alive = false;
        endingActual = p - cfg.spending * (total - 1 - t);
        path.push(0); for (let k = t + 1; k < total; k++) path.push(0);
        break;
      }
      const nominal = cfg.equity * stock + (1 - cfg.equity) * bond - cfg.fee;
      const real = (1 + nominal) / (1 + infl) - 1;
      p *= (1 + real); cum *= (1 + infl);
      path.push(p);
    }
    if (alive) { success++; endingActual = p; }
    trajectories.push({ start: M[s][0], alive, ending: p > 0 ? p : 0, endingActual, path });
  }
  const N = trajectories.length;
  const endings = trajectories.map(c => c.ending).sort((a, b) => a - b);
  const deepest = trajectories.reduce((a, b) => b.endingActual < a.endingActual ? b : a, trajectories[0]);
  return {
    cycles: N, success, failures: N - success, rate: N ? 100 * success / N : 0,
    lo: endings[0], hi: endings[N - 1], med: endings[Math.floor(N / 2)],
    avg: endings.reduce((a, b) => a + b, 0) / N,
    loActual: deepest.endingActual, worstStart: deepest.start,
    total, accum, retire, firstStart: trajectories[0].start, lastStart: trajectories[N - 1].start,
    trajectories,
  };
}

function retirementPanelHTML() {
  return `
  <h2>Retirement calculator</h2>
  <p class="foot" id="ret-intro"></p>
  <div class="ret-inputs">
    <div class="ret-grid">
      <div class="ret-field"><label>Portfolio (net worth)</label>
        <div class="ret-pfx"><span>$</span><input type="number" id="ret-portfolio" min="0" step="1000"></div></div>
      <div class="ret-field"><label>Annual spending</label>
        <div class="ret-pfx"><span>$</span><input type="number" id="ret-spending" min="0" step="1000"></div></div>
      <div class="ret-field"><label>Years to model (total)</label>
        <input type="number" id="ret-years" min="1" max="120" step="1"></div>
      <div class="ret-field"><label>% in stocks</label>
        <input type="number" id="ret-equity" min="0" max="100" step="1"></div>
      <div class="ret-field"><label>Expense ratio (%/yr)</label>
        <input type="number" id="ret-fee" min="0" max="5" step="0.01"></div>
    </div>
    <div class="ret-row">
      <label class="ret-check"><input type="checkbox" id="ret-retired"> Already retired</label>
      <div class="ret-accum" id="ret-accum">
        <div class="ret-field"><label>Retirement year</label>
          <input type="number" id="ret-retyear" step="1"></div>
        <div class="ret-field"><label>Annual savings (until retirement)</label>
          <div class="ret-pfx"><span>$</span><input type="number" id="ret-contrib" min="0" step="1000"></div></div>
      </div>
    </div>
    <div class="ret-lumps">
      <div class="ret-lumps-hd"><span>Lump-sum changes (inheritance, home purchase, …)</span>
        <button class="nw-refresh" id="ret-add-lump" type="button">+ Add</button></div>
      <div id="ret-lump-rows"></div>
    </div>
    <div class="ret-defaults">
      <span class="ret-defaults-lbl">Default values from:</span>
      <span class="ret-year-btns" id="ret-year-btns"></span>
    </div>
  </div>

  <h2>Results</h2>
  <div class="cards">
    <div class="card"><div class="lbl">Success rate</div><div class="val" id="ret-rate">—</div></div>
    <div class="card"><div class="lbl">Cycles tested</div><div class="val" id="ret-cycles">—</div></div>
    <div class="card"><div class="lbl">Average ending (today$)</div><div class="val" id="ret-avg">—</div></div>
  </div>
  <p class="foot" id="ret-detail"></p>
  <div class="chart-card ret-chart-card">
    <div class="ret-legend">
      <span><i class="lav"></i>Cycle</span>
      <span><i class="teal"></i>Median</span>
      <span><i class="pink"></i>Ran out</span>
    </div>
    <canvas id="ret-chart" height="150"></canvas>
  </div>
  <p class="foot" id="ret-note"></p>`;
}

function initRetirement(idx) {
  const RET_KEY = 'finance-dashboard-retirement';
  const $ = id => document.getElementById(id);
  const els = {
    portfolio: $('ret-portfolio'), spending: $('ret-spending'), years: $('ret-years'),
    equity: $('ret-equity'), fee: $('ret-fee'), retired: $('ret-retired'),
    accum: $('ret-accum'), retyear: $('ret-retyear'), contrib: $('ret-contrib'),
    lumpRows: $('ret-lump-rows'),
  };
  const thisYear = new Date().getFullYear();
  const num = el => { const v = parseFloat(String(el.value).replace(/[^0-9.\-]/g, '')); return isFinite(v) ? v : 0; };
  let retChart = null;

  function projNum(key) {
    const v = parseFloat(String((PANELS[0] && PANELS[0][key]) || '').replace(/[^0-9.]/g, ''));
    return isFinite(v) ? Math.round(v) : 0;
  }
  const projectedSpending = () => projNum('proj_spend');   // latest projected annual spending
  const projectedSavings = () => projNum('proj_sav');      // latest projected annual savings
  function addLump(d) {
    d = d || {};
    const row = document.createElement('div');
    row.className = 'ret-lump-row';
    row.innerHTML = '<select class="lump-type"><option value="add">Add</option>'
      + '<option value="wd">Withdraw</option></select><span>$</span>'
      + '<input type="number" class="lump-amt" min="0" step="1000" placeholder="amount">'
      + '<span>in</span><input type="number" class="lump-year" step="1" placeholder="year" style="width:90px">'
      + '<label class="ret-check"><input type="checkbox" class="lump-infl"> today$</label>'
      + '<button class="nw-refresh lump-del" type="button">remove</button>';
    row.querySelector('.lump-type').value = d.add === false ? 'wd' : 'add';
    row.querySelector('.lump-amt').value = d.amount != null ? d.amount : '';
    row.querySelector('.lump-year').value = d.year != null ? d.year : thisYear + 1;
    row.querySelector('.lump-infl').checked = d.inflAdj !== false;
    row.querySelector('.lump-del').onclick = () => { row.remove(); recompute(); };
    row.querySelectorAll('input,select').forEach(e => e.addEventListener('input', recompute));
    els.lumpRows.appendChild(row);
  }
  function readLumps() {
    return [...els.lumpRows.querySelectorAll('.ret-lump-row')].map(r => ({
      add: r.querySelector('.lump-type').value === 'add',
      amount: Math.abs(num(r.querySelector('.lump-amt'))),
      year: parseInt(r.querySelector('.lump-year').value) || thisYear,
      inflAdj: r.querySelector('.lump-infl').checked,
    })).filter(l => l.amount > 0);
  }
  function readCfg() {
    return {
      portfolio: num(els.portfolio), spending: num(els.spending),
      years: Math.max(1, Math.round(num(els.years))),
      equity: Math.min(1, Math.max(0, num(els.equity) / 100)),
      fee: Math.max(0, num(els.fee) / 100),
      retiredNow: els.retired.checked,
      retireYear: Math.round(num(els.retyear)) || thisYear,
      contribution: num(els.contrib),
      lumps: readLumps(), thisYear,
    };
  }
  function saveState() {
    const st = {
      portfolio: els.portfolio.value, spending: els.spending.value, years: els.years.value,
      equity: els.equity.value, fee: els.fee.value, retired: els.retired.checked,
      retyear: els.retyear.value, contrib: els.contrib.value, lumps: readLumps(),
    };
    try { localStorage.setItem(RET_KEY, JSON.stringify(st)); } catch (e) {}
  }

  function render(cfg, res) {
    const rate = $('ret-rate'), cyc = $('ret-cycles'), avg = $('ret-avg'),
      detail = $('ret-detail'), note = $('ret-note');
    if (!res || res.tooLong || res.badAccum || !res.cycles) {
      rate.textContent = '—'; cyc.textContent = '—'; avg.textContent = '—';
      detail.textContent = res && res.tooLong
        ? 'Years to model (' + res.total + ') exceeds the ' + MARKET.length
          + ' years of historical data — lower it.'
        : res && res.badAccum
        ? 'Your retirement year is at/after the end of the modeled horizon — increase '
          + 'Years to model or retire earlier.'
        : 'Enter your inputs above to run the simulation.';
      if (retChart) { try { retChart.destroy(); } catch (e) {} retChart = null; }
      note.textContent = '';
      return;
    }
    rate.textContent = res.rate.toFixed(1) + '%';
    rate.style.color = res.rate >= 95 ? 'var(--accent2)' : res.rate >= 80 ? '#e0b341' : '#ff6b6b';
    cyc.textContent = res.cycles;
    avg.textContent = fmt(res.avg);
    detail.innerHTML = 'Tested <b>' + res.cycles + '</b> historical '
      + (res.accum ? res.accum + '-yr accumulation + ' : '') + res.retire + '-yr retirement ('
      + res.firstStart + '–' + res.lastStart + ' starts); <b>' + res.failures + '</b> ran out. '
      + 'Ending balance (today$): low ' + fmt(res.loActual) + ' · median ' + fmt(res.med)
      + ' · avg ' + fmt(res.avg) + ' · high ' + fmt(res.hi)
      + '. Worst start year: <b>' + res.worstStart + '</b> (deepest shortfall).';

    const labels = res.trajectories[0].path.map((_, i) => cfg.thisYear + i);
    const datasets = res.trajectories.map(c => ({
      data: c.path, borderWidth: 1, pointRadius: 0, tension: 0, fill: false,
      borderColor: c.alive ? 'rgba(168,148,255,0.22)' : 'rgba(255,95,162,0.55)',
    }));
    const byEnd = [...res.trajectories].sort((a, b) => a.ending - b.ending);
    datasets.push({ data: byEnd[Math.floor(byEnd.length / 2)].path, borderColor: '#39d3bb',
      borderWidth: 2, pointRadius: 0, tension: 0, fill: false });
    if (retChart) {
      try { retChart.destroy(); } catch (e) {}
      const i = chartInstances.indexOf(retChart); if (i >= 0) chartInstances.splice(i, 1);
    }
    retChart = new Chart($('ret-chart'), {
      type: 'line', data: { labels, datasets },
      options: {
        animation: false, plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: {
          x: { ticks: { color: '#9aa0ad', maxTicksLimit: 12 }, grid: { color: '#262a36' } },
          y: { ticks: { color: '#9aa0ad', callback: v => fmt(v) }, grid: { color: '#262a36' }, beginAtZero: true },
        },
      },
    });
    chartInstances.push(retChart);
    note.textContent = 'Data: S&P 500 + 10-yr Treasury total returns and CPI, '
      + MARKET[0][0] + '–' + MARKET[MARKET.length - 1][0] + '. Each faint line is one historical '
      + 'start year applied to your plan; teal = median outcome, pink = ran out. Today’s dollars.';
  }

  let debounce;
  function recompute() {
    els.accum.style.display = els.retired.checked ? 'none' : '';
    clearTimeout(debounce);
    debounce = setTimeout(() => { const cfg = readCfg(); saveState(); render(cfg, simulate(cfg)); }, 120);
  }

  // Always ingest the LATEST Net Worth / Spending / Savings on every load (these
  // reflect live data and are not restored from saved state). Years, allocation,
  // fee, retirement year, and lump sums stay user-tunable and persist.
  let st = null; try { st = JSON.parse(localStorage.getItem(RET_KEY)); } catch (e) {}
  st = st || {};
  els.spending.value = projectedSpending() || '';           // latest projected spending
  els.contrib.value = projectedSavings() || '';             // latest projected savings
  els.portfolio.value = '';                                 // filled from latest net worth below
  els.years.value = st.years || 60;
  els.equity.value = st.equity != null && st.equity !== '' ? st.equity : 75;
  els.fee.value = st.fee != null && st.fee !== '' ? st.fee : 0.18;
  els.retired.checked = st.retired != null ? st.retired : false;
  els.retyear.value = st.retyear || (thisYear + 10);
  (st.lumps || []).forEach(addLump);

  $('ret-intro').textContent = 'A Monte Carlo-style simulation: your plan is run through '
    + 'every historical market sequence since ' + (MARKET[0] ? MARKET[0][0] : '') + '. '
    + 'Net worth, spending & savings are pulled from your latest synced data each '
    + 'time you open the page; change anything to explore what-ifs.';

  els.retired.addEventListener('change', recompute);
  [els.portfolio, els.spending, els.years, els.equity, els.fee, els.retyear, els.contrib]
    .forEach(e => e.addEventListener('input', recompute));
  $('ret-add-lump').addEventListener('click', () => { addLump(); recompute(); });

  // "Default values from:" — one button per Save/Spend year. Clicking pulls that
  // year's projected annual spending + savings (incl 401k) into the calculator.
  (function () {
    const wrap = $('ret-year-btns');
    if (!wrap || typeof PANELS === 'undefined') return;
    const money = s => { const v = parseFloat(String(s == null ? '' : s).replace(/[^0-9.]/g, '')); return isFinite(v) ? Math.round(v) : 0; };
    PANELS.forEach(p => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ret-year-btn';
      b.textContent = p.year;
      b.addEventListener('click', () => {
        els.spending.value = money(p.proj_spend) || '';
        els.contrib.value = money(p.proj_sav) || '';
        recompute();
      });
      wrap.appendChild(b);
    });
  })();

  // Portfolio = latest synced Net Worth (ingested fresh every load).
  function fillNetWorth() {
    if (window.__networth != null) { els.portfolio.value = Math.round(window.__networth); recompute(); return; }
    fetch('/networth.json', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).then(d => {
      if (d && d.networth != null) { els.portfolio.value = Math.round(d.networth); recompute(); }
    }).catch(() => {});
  }
  fillNetWorth();
  recompute();
}

// Build tab buttons + panel shells, but defer chart/table init until a panel
// is first shown. Chart.js animates from a zero-size canvas if it's created
// while display:none, so initializing on reveal makes every tab grow in with
// the same animation the first (visible) tab gets on load. renderAll() rebuilds
// everything (destroying old charts first) so a settings change can re-render.
const initialized = new Set();
let activeTop = 'savespend';   // 'savespend' | 'retire'
let activeYearIdx = 0;         // which year subtab under Save/Spend

function initOnce(key, fn) { if (!initialized.has(key)) { fn(); initialized.add(key); } }
function showOnePanel(id) {
  panelsEl.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === id));
}
function showYear(idx) {
  activeYearIdx = idx;
  subtabsEl.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active', i === idx));
  showOnePanel('panel-' + idx);
  initOnce('year-' + idx, () => initPanel(PANELS[idx], idx));
}
function showTop(top) {
  activeTop = top;
  tabsEl.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.top === top));
  subtabsEl.style.display = top === 'savespend' ? '' : 'none';
  if (top === 'retire') {
    showOnePanel('panel-retire');
    initOnce('retire', () => initRetirement());
  } else {
    showYear(activeYearIdx);
  }
}

function renderAll(panels) {
  chartInstances.forEach(c => { try { c.destroy(); } catch (e) {} });
  chartInstances.length = 0;
  initialized.clear();
  tabsEl.innerHTML = '';
  subtabsEl.innerHTML = '';
  panelsEl.innerHTML = '';
  PANELS = panels;

  const primary = PANELS[0];
  document.getElementById('mainTitle').textContent = 'FinProject';
  document.getElementById('sub').textContent =
    'Amount saved & spent per month · ' + primary.year + ' data through '
    + primary.data.last_month_label + ' · select a year tab to switch views';

  // Top-level tabs: Save/Spend (holds the year subtabs) and Retirement.
  [['savespend', 'Save/Spend'], ['retire', 'Retirement']].forEach(([top, label]) => {
    const b = document.createElement('button');
    b.className = 'tab-btn';
    b.textContent = label;
    b.dataset.top = top;
    tabsEl.appendChild(b);
  });

  // Year subtabs (under Save/Spend) + their panels.
  PANELS.forEach((P, idx) => {
    const btn = document.createElement('button');
    btn.className = 'tab-btn';
    btn.textContent = P.label;
    btn.dataset.idx = idx;
    subtabsEl.appendChild(btn);

    const panel = document.createElement('div');
    panel.className = 'panel';
    panel.id = 'panel-' + idx;
    panel.innerHTML = panelHTML(P, idx);
    panelsEl.appendChild(panel);
  });

  // Retirement panel.
  const rPanel = document.createElement('div');
  rPanel.className = 'panel';
  rPanel.id = 'panel-retire';
  rPanel.innerHTML = retirementPanelHTML();
  panelsEl.appendChild(rPanel);

  if (activeYearIdx >= PANELS.length) activeYearIdx = 0;
  showTop(activeTop);
}

tabsEl.addEventListener('click', e => {
  const btn = e.target.closest('.tab-btn');
  if (!btn) return;
  showTop(btn.dataset.top);
});
subtabsEl.addEventListener('click', e => {
  const btn = e.target.closest('.tab-btn');
  if (!btn) return;
  showYear(+btn.dataset.idx);
});

// ── Settings panel ──────────────────────────────────────────────────────────
// Defaults come from the Python run; overrides persist in localStorage and drive
// a live client-side recompute (computePanels). When settings equal the defaults
// we render the authoritative Python panels verbatim.
const SETTINGS_DEFAULTS = %%SETTINGS_JSON%%;
const SETTINGS_KEY = 'finance-dashboard-settings-' + PANELS_PY[0].year;

function loadSettings() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch (e) {}
  return Object.assign({}, SETTINGS_DEFAULTS, saved);
}
function sameAsDefaults(s) {
  return ['rent','biweekly_deposit','k401_annual','receive_bonuses','bonus_threshold']
    .every(k => s[k] === SETTINGS_DEFAULTS[k]);
}
function panelsFor(s) { return sameAsDefaults(s) ? PANELS_PY : computePanels(s); }
function applySettings(s) { renderAll(panelsFor(s)); }

const gearBtn = document.getElementById('gearBtn');
const overlay = document.getElementById('settingsOverlay');
const elRent = document.getElementById('setRent');
const elBiweek = document.getElementById('setBiweek');
const elK401 = document.getElementById('setK401');
const elBonus = document.getElementById('setBonus');
const elBonusThreshold = document.getElementById('setBonusThreshold');
const bonusThresholdField = document.getElementById('bonusThresholdField');

function syncBonusVisibility() {
  bonusThresholdField.style.display = elBonus.checked ? '' : 'none';
}
function fillForm(s) {
  elRent.value = s.rent;
  elBiweek.value = s.biweekly_deposit;
  elK401.value = s.k401_annual;
  elBonus.checked = !!s.receive_bonuses;
  elBonusThreshold.value = s.bonus_threshold;
  syncBonusVisibility();
}
function openSettings() {
  fillForm(loadSettings());
  overlay.classList.add('open');
}
function closeSettings() { overlay.classList.remove('open'); }

gearBtn.addEventListener('click', openSettings);
document.getElementById('cancelSettings').addEventListener('click', closeSettings);
overlay.addEventListener('click', e => { if (e.target === overlay) closeSettings(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSettings(); });
elBonus.addEventListener('change', syncBonusVisibility);
// Full reset: clear browser state (settings + retirement inputs) and, via the
// bridge, the saved baselines + net worth + Empower session, then reload into
// first-run onboarding. Over file:// the bridge call fails but localStorage is
// still cleared.
document.getElementById('resetAll').addEventListener('click', () => {
  if (!confirm('Reset and start over? This clears your settings, saved baselines, '
      + 'and synced net worth, and re-runs first-time setup. Your loaded '
      + 'transactions are kept.')) return;
  const finish = () => { try { localStorage.clear(); } catch (e) {} location.href = location.pathname; };
  fetch('/api/reset', {method: 'POST'}).then(finish).catch(finish);
});

// Top-left reset button: FULL wipe — loaded transactions, prior-year history,
// net worth, retirement projection (localStorage), and baseline config. Returns
// to the empty Welcome page.
const resetBtnEl = document.getElementById('resetBtn');
if (resetBtnEl) resetBtnEl.addEventListener('click', () => {
  if (!confirm('Reset everything? This permanently clears your loaded transactions, '
      + 'prior-year history, net worth, retirement projection, and baseline settings. '
      + 'This cannot be undone.')) return;
  resetBtnEl.disabled = true;
  const finish = () => { try { localStorage.clear(); } catch (e) {} location.href = '/'; };
  fetch('/api/reset?all=1', {method: 'POST'}).then(finish).catch(finish);
});

const saveBtn = document.getElementById('saveSettings');
saveBtn.addEventListener('click', () => {
  const s = {
    rent: parseFloat(elRent.value) || 0,
    biweekly_deposit: parseFloat(elBiweek.value) || 0,
    k401_annual: parseFloat(elK401.value) || 0,
    receive_bonuses: elBonus.checked,
    bonus_threshold: parseFloat(elBonusThreshold.value) || 0,
  };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  closeSettings();
  applySettings(s);   // live recompute + re-render
});

// Re-open the transaction mapping screen (defined below) from Settings so the
// user can re-sort groups without re-importing.
document.getElementById('editMapping').addEventListener('click', () => {
  closeSettings();
  openMapping();
});

// ── Transaction mapping (3-column drag sort) ────────────────────────────────
// Groups are dragged between Spending / Savings & Investing / Income. Saving
// POSTs group_buckets to the bridge, which re-classifies (server-side) and we
// reload. Venmo/gambling groups are auto-netted and shown read-only.
const mapOverlay = document.getElementById('mappingOverlay');
let mapDragEl = null;
let mapReloadOnCancel = false;   // opened after a data load (page not yet refreshed)
const MAP_GRIP = '<svg class="mc-grip" width="8" height="14" viewBox="0 0 8 14" aria-hidden="true">'
  + '<g fill="currentColor"><circle cx="2" cy="2" r="1.2"/><circle cx="6" cy="2" r="1.2"/>'
  + '<circle cx="2" cy="7" r="1.2"/><circle cx="6" cy="7" r="1.2"/>'
  + '<circle cx="2" cy="12" r="1.2"/><circle cx="6" cy="12" r="1.2"/></g></svg>';
function mapMoney(n) {
  return (n < 0 ? '-$' : '+$') + Math.abs(Math.round(n)).toLocaleString();
}
function mapCard(g) {
  const el = document.createElement('div');
  el.className = 'map-card';
  el.draggable = true;
  el.dataset.key = g.key;
  el.dataset.net = g.net;
  el.dataset.count = g.count;
  const body = document.createElement('div'); body.className = 'mc-body';
  const lab = document.createElement('div'); lab.className = 'mc-label'; lab.textContent = g.label; lab.title = g.label;
  const meta = document.createElement('div'); meta.className = 'mc-meta';
  meta.textContent = g.count + (g.count === 1 ? ' txn · net ' : ' txns · net ') + mapMoney(g.net);
  body.appendChild(lab); body.appendChild(meta);
  el.innerHTML = MAP_GRIP;
  el.appendChild(body);
  el.addEventListener('dragstart', e => {
    mapDragEl = el; el.classList.add('dragging');
    try { e.dataTransfer.setData('text/plain', g.key); e.dataTransfer.effectAllowed = 'move'; } catch (_) {}
  });
  el.addEventListener('dragend', () => { el.classList.remove('dragging'); mapDragEl = null; });
  return el;
}
function updateSums() {
  ['spending', 'savings', 'income'].forEach(b => {
    let net = 0, count = 0;
    document.querySelectorAll('#col-' + b + ' .map-card').forEach(c => {
      net += parseFloat(c.dataset.net) || 0; count += parseInt(c.dataset.count) || 0;
    });
    const box = document.getElementById('sum-' + b);
    box.querySelector('.ms-count').textContent = count + (count === 1 ? ' txn' : ' txns');
    box.querySelector('.ms-total').textContent = mapMoney(net);
  });
}
function renderMapping(groups) {
  ['spending', 'savings', 'income'].forEach(b => { document.getElementById('col-' + b).innerHTML = ''; });
  (groups || []).forEach(g => {
    if (g.auto) return;   // Venmo/gambling are auto-netted — not shown or sortable
    const b = g.bucket && g.bucket !== 'auto' ? g.bucket : (g.default_bucket || 'spending');
    (document.getElementById('col-' + b) || document.getElementById('col-spending')).appendChild(mapCard(g));
  });
  updateSums();
}
document.querySelectorAll('.map-drop').forEach(zone => {
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('over');
    if (mapDragEl) { zone.appendChild(mapDragEl); updateSums(); }
  });
});
function collectMapping() {
  const m = {};
  document.querySelectorAll('.map-drop').forEach(zone => {
    zone.querySelectorAll('.map-card').forEach(c => { m[c.dataset.key] = zone.dataset.bucket; });
  });
  return m;
}
function openMapping(groups, reloadOnCancel) {
  mapReloadOnCancel = !!reloadOnCancel;
  if (groups) { renderMapping(groups); mapOverlay.classList.add('open'); return; }
  fetch('/api/groups').then(r => r.json()).then(d => {
    renderMapping(d.groups || []); mapOverlay.classList.add('open');
  }).catch(() => {});
}
document.getElementById('cancelMapping').addEventListener('click', () => {
  mapOverlay.classList.remove('open');
  // Opened right after a data load — the dashboard behind is stale/placeholder,
  // so reload to move on (to the freshly-loaded data, then onboarding if needed).
  if (mapReloadOnCancel) location.reload();
});
document.getElementById('doneMapping').addEventListener('click', () => {
  const btn = document.getElementById('doneMapping');
  btn.disabled = true; btn.textContent = 'Saving…';
  fetch('/api/config', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({group_buckets: collectMapping()})})
    .then(r => r.json()).then(() => location.reload())
    .catch(() => location.reload());
});

// ── First-run onboarding ─────────────────────────────────────────────────────
// When the machine has no finance_config.json (fresh clone), the Python side
// sets NEEDS_ONBOARDING and embeds placeholder settings. We block the dashboard
// behind a short questionnaire, then persist the answers to finance_config.json
// via the local bridge (POST /api/config) so future CLI runs pick them up, and
// recompute the panels client-side from the entered values.
const NEEDS_ONBOARDING = %%NEEDS_ONBOARDING%%;
// The welcome page's "Build dashboard" (and a fresh import) send us here with
// ?map=1 so we open the transaction-sorting screen once the page is ready
// (after onboarding, if that's also showing). Clean the URL so a refresh won't.
const wantMapping = new URLSearchParams(location.search).has('map');
if (wantMapping) { try { history.replaceState(null, '', location.pathname); } catch (e) {} }
const obOverlay = document.getElementById('onboardingOverlay');
const obRent = document.getElementById('obRent');
const obHasOther = document.getElementById('obHasOther');
const obBiweekField = document.getElementById('obBiweekField');
const obBiweek = document.getElementById('obBiweek');
const obK401 = document.getElementById('obK401');
const obBonus = document.getElementById('obBonus');
const obThresholdField = document.getElementById('obThresholdField');
const obThreshold = document.getElementById('obThreshold');
const obFinish = document.getElementById('finishOnboarding');

function obValidate() {
  const ok = obRent.value !== '' && obK401.value !== ''
    && (!obHasOther.checked || obBiweek.value !== '')
    && (!obBonus.checked || obThreshold.value !== '');
  obFinish.disabled = !ok;
}
obHasOther.addEventListener('change', () => {
  obBiweekField.style.display = obHasOther.checked ? '' : 'none';
  obValidate();
});
obBonus.addEventListener('change', () => {
  obThresholdField.style.display = obBonus.checked ? '' : 'none';
  obValidate();
});
[obRent, obBiweek, obK401, obThreshold].forEach(el => el.addEventListener('input', obValidate));

function hasSavedSettings() {
  try { return !!JSON.parse(localStorage.getItem(SETTINGS_KEY)); } catch (e) { return false; }
}

obFinish.addEventListener('click', () => {
  const s = {
    rent: parseFloat(obRent.value) || 0,
    biweekly_deposit: obHasOther.checked ? (parseFloat(obBiweek.value) || 0) : 0,
    k401_annual: parseFloat(obK401.value) || 0,
    receive_bonuses: obBonus.checked,
    bonus_threshold: obBonus.checked ? (parseFloat(obThreshold.value) || 0) : 0,
  };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  // Persist to finance_config.json so the CLI/report use it next time (best
  // effort — localStorage already drives this page even if the bridge is down).
  fetch('/api/config', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(s)}).catch(() => {});
  obOverlay.classList.remove('open');
  applySettings(s);
});

function normalGate() {
  if (NEEDS_ONBOARDING && !hasSavedSettings()) {
    obValidate();
    obOverlay.classList.add('open');
    renderAll(PANELS_PY);   // placeholder render behind the overlay
  } else {
    applySettings(loadSettings());   // honors any previously-saved overrides
  }
}
// Gate. When we arrived from a data load (?map=1), sort transactions FIRST — but
// only if the file introduced net-new groups; otherwise skip straight to the
// normal gate. After the mapping's Done/Cancel reload (no ?map) we fall through
// to onboarding if baselines are still missing.
if (wantMapping) {
  renderAll(PANELS_PY);   // placeholder behind whatever comes next
  fetch('/api/groups').then(r => r.json()).then(d => {
    const newGroups = (d.groups || []).filter(g => !g.auto && !g.mapped);
    if (newGroups.length) openMapping(newGroups, true);
    else normalGate();
  }).catch(normalGate);
} else {
  normalGate();
}

// ── Net worth card ────────────────────────────────────────────────────────────
// Talks to the local bridge server (dashboard_server.py): reads /networth.json,
// and the "Sync" button POSTs /api/sync (which runs the Empower browser fetch)
// then polls /api/sync-status until the value lands. Over file:// (no server)
// these fetches fail and the card explains how to start the bridge.
(function () {
  const valEl = document.getElementById('nwVal');
  const metaEl = document.getElementById('nwMeta');
  const actEl = document.getElementById('nwActions');
  const barWrap = document.getElementById('nwBarWrap');
  const STALE_MS = 7 * 24 * 3600 * 1000;
  let polling = null;

  const money = n => '$' + Math.round(n).toLocaleString('en-US');

  function ageInfo(iso) {
    const d = new Date(iso);
    const days = Math.floor((Date.now() - d.getTime()) / 86400000);
    return {
      dateStr: d.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'}),
      stale: (Date.now() - d.getTime()) > STALE_MS,
      rel: days <= 0 ? 'today' : days === 1 ? '1 day ago' : days + ' days ago',
    };
  }
  function button(cls, label, fn) {
    actEl.innerHTML = '';
    const b = document.createElement('button');
    b.className = cls; b.textContent = label; b.onclick = fn;
    actEl.appendChild(b);
    return b;
  }
  function renderBar(c) {
    if (!c) { barWrap.innerHTML = ''; return; }
    const segs = [
      {cls: 'teal',   label: 'Cash',         amt: +c.cash || 0},
      {cls: 'purple', label: 'Investments',  amt: +c.investments || 0},
      {cls: 'pink',   label: 'Credit Cards', amt: +c.credit_cards || 0},
    ].filter(s => Math.abs(s.amt) > 0.5);
    if (!segs.length) { barWrap.innerHTML = ''; return; }
    const seg = s => {
      const txt = (s.amt < 0 ? '-' : '') + '$'
        + Math.round(Math.abs(s.amt)).toLocaleString('en-US');
      return '<div class="nw-seg ' + s.cls + '" style="flex-grow:' + Math.abs(s.amt)
        + '" title="' + s.label + ': ' + txt + '">' + txt + '</div>';
    };
    const lg = s => '<span class="nw-lg"><i class="' + s.cls + '"></i>' + s.label + '</span>';
    barWrap.innerHTML = '<div class="nw-bar">' + segs.map(seg).join('') + '</div>'
      + '<div class="nw-legend">' + segs.map(lg).join('') + '</div>';
  }
  function showValue(data) {
    window.__nwComponents = data.components || null;   // used by the Retirement tab default
    window.__networth = data.networth;                 // portfolio basis for the Retirement tab
    valEl.textContent = money(data.networth);
    const a = ageInfo(data.fetched_at);
    metaEl.textContent = 'Synced from Empower · as of ' + a.dateStr + ' (' + a.rel + ')'
      + (a.stale ? ' — stale, refresh recommended' : '');
    metaEl.className = 'nw-meta' + (a.stale ? ' stale' : '');
    renderBar(data.components);
    button('nw-refresh', 'Refresh', startSync);
  }
  function showNeedsSync(msg) {
    valEl.textContent = '—';
    metaEl.textContent = msg; metaEl.className = 'nw-meta';
    barWrap.innerHTML = '';
    button('nw-btn', 'Sync net worth', startSync);
  }
  function showSyncing() {
    metaEl.textContent = 'Opening Empower in Chrome — finish login there if prompted…';
    metaEl.className = 'nw-meta';
    const b = button('nw-btn', 'Syncing…', () => {}); b.disabled = true;
  }
  function showError(msg) {
    metaEl.textContent = 'Sync failed: ' + (msg || 'unknown error');
    metaEl.className = 'nw-meta stale';
    button('nw-btn', 'Try again', startSync);
  }
  function showNoServer() {
    valEl.textContent = '—';
    metaEl.innerHTML = 'Start the local bridge to sync: '
      + '<code>.venv/bin/python dashboard_server.py</code>';
    metaEl.className = 'nw-meta';
    barWrap.innerHTML = '';
    actEl.innerHTML = '';
  }

  function startSync() {
    showSyncing();
    fetch('/api/sync', {method: 'POST'})
      .then(() => poll())
      .catch(() => showNoServer());
  }
  function poll() {
    if (polling) clearInterval(polling);
    polling = setInterval(() => {
      fetch('/api/sync-status', {cache: 'no-store'}).then(r => r.json()).then(s => {
        if (s.state === 'done') { clearInterval(polling); init(); }
        else if (s.state === 'error') { clearInterval(polling); showError(s.message); }
      }).catch(() => { clearInterval(polling); showNoServer(); });
    }, 2000);
  }
  function init() {
    fetch('/networth.json', {cache: 'no-store'})
      .then(r => {
        if (r.status === 404) { showNeedsSync('Not synced yet — pull your net worth from Empower.'); return null; }
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      })
      .then(d => { if (d) showValue(d); })
      .catch(() => showNoServer());
  }
  init();
})();

// ── Power button: stop the bridge and close the tab ──────────────────────────
(function () {
  const pb = document.getElementById('powerBtn');
  if (!pb) return;
  pb.addEventListener('click', () => {
    pb.disabled = true;
    fetch('/api/shutdown', { method: 'POST' }).catch(() => {}).finally(() => {
      window.close();  // works if the tab was opened by a script; otherwise no-op
      setTimeout(() => {
        document.documentElement.innerHTML = '<body style="margin:0;min-height:100vh;'
          + 'display:flex;align-items:center;justify-content:center;background:#0f1117;'
          + 'color:#9aa0ad;font:15px -apple-system,BlinkMacSystemFont,sans-serif">'
          + 'Server stopped — you can close this tab.</body>';
      }, 300);
    });
  });
})();

// ── Load transactions (drag-drop / paste) ────────────────────────────────────
// Posts the file text to the bridge's /api/ingest, which rewrites the CSV, re-runs
// project.py to rebuild the dashboard, then the page reloads. Needs the bridge
// (dashboard_server.py); over file:// the fetch fails with a hint.
(function () {
  const btn = document.getElementById('importBtn');
  const ov = document.getElementById('importOverlay');
  const drop = document.getElementById('importDrop');
  const fileEl = document.getElementById('importFile');
  const pasteEl = document.getElementById('importPaste');
  const msgEl = document.getElementById('importMsg');
  const goEl = document.getElementById('importGo');
  const dropDefault = drop.innerHTML;
  let pending = '';

  function esc(s) { return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function extLabel(name) { const m = /\.([a-z0-9]+)$/i.exec(name); return (m ? m[1].toUpperCase() : 'FILE').slice(0, 4); }
  function showFile(name) {
    drop.classList.add('has-file');
    drop.innerHTML = '<div class="filechip">'
      + '<svg width="30" height="36" viewBox="0 0 30 36" fill="none" aria-hidden="true">'
      + '<path d="M6.6 0H19l9 9v24.4A2.6 2.6 0 0 1 25.4 36H6.6A2.6 2.6 0 0 1 4 33.4V2.6A2.6 2.6 0 0 1 6.6 0Z" fill="#7c5cff" fill-opacity=".16" stroke="#7c5cff" stroke-width="1.6"/>'
      + '<path d="M19 0v7a2 2 0 0 0 2 2h7" stroke="#7c5cff" stroke-width="1.6" fill="none"/>'
      + '<text x="16" y="26" text-anchor="middle" font-size="7.5" font-weight="700" fill="#7c5cff" font-family="ui-monospace,Menlo,monospace">' + esc(extLabel(name)) + '</text>'
      + '</svg><div class="fc-name">' + esc(name) + '</div></div>'
      + '<div class="fc-hint">Click to choose a different file</div>';
  }
  function resetDrop() { drop.classList.remove('has-file'); drop.innerHTML = dropDefault; }

  function openM() { pending = ''; pasteEl.value = ''; msgEl.textContent = ''; goEl.disabled = false; resetDrop(); ov.classList.add('open'); }
  function closeM() { ov.classList.remove('open'); }
  function readFile(f) {
    const r = new FileReader();
    r.onload = () => { pending = r.result; showFile(f.name); msgEl.textContent = f.name + ' ready'; };
    r.readAsText(f);
  }
  btn.addEventListener('click', openM);
  document.getElementById('importCancel').addEventListener('click', closeM);
  ov.addEventListener('click', e => { if (e.target === ov) closeM(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeM(); });
  drop.addEventListener('click', () => fileEl.click());
  fileEl.addEventListener('change', e => { if (e.target.files[0]) readFile(e.target.files[0]); });
  ['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => { if (e.dataTransfer.files[0]) readFile(e.dataTransfer.files[0]); });

  goEl.addEventListener('click', () => {
    const text = pending || pasteEl.value.trim();
    if (!text) { msgEl.textContent = 'Drop a file or paste rows first.'; return; }
    goEl.disabled = true;
    msgEl.textContent = 'Loading & rebuilding…';
    fetch('/api/ingest', {method: 'POST', headers: {'Content-Type': 'text/plain'}, body: text})
      .then(r => r.json()).then(d => {
        if (d.ok) {
          const newGroups = (d.groups || []).filter(g => !g.auto && !g.mapped);
          if (d.needs_current) { msgEl.textContent = d.summary || 'Loaded'; goEl.disabled = false; }
          else if (newGroups.length) {
            // Only prompt for the net-new groups; already-sorted ones are kept.
            ov.classList.remove('open');
            openMapping(newGroups, true);
          } else {
            // No new categories to sort — go straight to the dashboard.
            msgEl.textContent = (d.summary || ('Loaded ' + d.rows + ' rows')) + ' — refreshing…';
            setTimeout(() => location.reload(), 900);
          }
        }
        else { goEl.disabled = false; msgEl.textContent = 'Error: ' + (d.error || 'failed'); }
      }).catch(() => { goEl.disabled = false; msgEl.textContent = 'No bridge running — start dashboard_server.py.'; });
  });
})();
</script>
</body>
</html>
"""


def build_panel(
    res: Result,
    monthly: dict,
    biweekly_deposit: float,
    rent_monthly: float,
    k401_annual: float,
    label: str | None = None,
    historical: bool = False,
) -> dict:
    """Build one tab's payload (cards + chart/table data) for the dashboard.

    historical=True marks a completed year: the projection cards are dropped
    since every month is actual."""
    year = res.year_start.year
    as_of = res.as_of
    last = monthly["last_month"]
    ytd_heading = (f"YTD (Jan 1 – {MONTHS[as_of.month-1]} "
                   f"{as_of.day}, {year})")

    # Projected future months (last_month+1 .. Dec), kept in separate *_proj
    # arrays so the table/footer totals below stay YTD-only. Income-driven
    # pieces (savings, 401k, the biweekly budget) are distributed by each
    # month's projected paycheck count, so a 3-paycheck month shows its bump;
    # rent is fixed per month and the variable remainder is spread evenly.
    # For a completed year (last == 12) there are no projected months.
    proj_last = 12 if (last and last < 12) else last
    n_proj = proj_last - last
    excl_proj = list(monthly["excl"])
    k401_proj = list(monthly["k401"])
    actual_proj = list(monthly["actual"])
    expected_proj = list(monthly["expected"])
    paychecks_proj = list(monthly["paychecks"])
    proj_pay = monthly.get("proj_paychecks", [0] * 12)

    # Mid-month split: the current month (index last-1) is only partially
    # elapsed, so carve out the projected remainder of that month into separate
    # *_rem arrays. The solid bars keep showing actual-so-far; these feed the
    # translucent cap stacked on top for the in-progress month only.
    excl_rem = [0.0] * 12
    k401_rem = [0.0] * 12
    rent_rem = [0.0] * 12
    other_rem = [0.0] * 12
    cur = last - 1  # 0-based current-month index
    cur_frac = 0.0  # fraction of the current month still remaining
    # Only the last data month that is *also* the current calendar month is
    # "in progress". If the paste is stale (data ends in an earlier month than
    # as_of), that month is complete — render it fully solid, no projected cap.
    if last and not historical and last == as_of.month:
        days_in_month = calendar.monthrange(year, last)[1]
        days_left = days_in_month - as_of.day
        if days_left > 0 and as_of < date(year, 12, 31):
            cur_frac = days_left / days_in_month

    if last and not historical and (n_proj > 0 or cur_frac > 0):
        # Paychecks: rest-of-current-month + each future month. Income-driven
        # pieces are weighted by paychecks; rent/other by elapsed month-units
        # (the current month counts only its remaining fraction).
        cur_pays = proj_pay[cur] if cur_frac > 0 else 0
        future_pays = sum(proj_pay[last:proj_last])
        all_pays = cur_pays + future_pays or (n_proj + cur_frac) or 1
        month_units = n_proj + cur_frac  # total remaining "months" of run-rate

        proj_excl = res.proj_savings_incl_401k - k401_annual
        rem_excl = proj_excl - res.savings_excl_401k
        rem_k401 = k401_annual - res.k401_ytd
        rem_spend = res.proj_spending - res.spending
        # Decompose remaining spend: fixed rent + paycheck-driven budget + rest.
        # Rent is prepaid in full at the start of each month, so the in-progress
        # month's rent is already paid (captured in the totals) — only the
        # fully-future months (n_proj) still owe rent. Discretionary "other"
        # still accrues over time, so it's distributed across month_units.
        rem_rent = rent_monthly * n_proj
        rem_budget = biweekly_deposit * all_pays
        rem_other = rem_spend - rem_rent - rem_budget

        # Rest of the current (partial) month → translucent cap.
        if cur_frac > 0:
            wt = (cur_pays / all_pays) if all_pays else 0
            excl_rem[cur] = round(rem_excl * wt, 2)
            k401_rem[cur] = round(rem_k401 * wt, 2)
            # Rent is prepaid in full, so the in-progress month owes no projected
            # rent — its rent shows fully solid, not a pro-rated translucent cap.
            rent_rem[cur] = 0.0
            other_rem[cur] = round(biweekly_deposit * cur_pays
                                   + (rem_other * cur_frac / month_units
                                      if month_units else 0), 2)
            paychecks_proj[cur] = monthly["paychecks"][cur] + cur_pays
            # Budget line = rent + $BIWEEK × pay periods that month. Base it on
            # the full-month projected pay count (actual so far + rest-of-month
            # projected), so the in-progress month's budget matches its siblings
            # instead of dropping to rent-only when no paycheck has landed yet.
            expected_proj[cur] = round(biweekly_deposit * paychecks_proj[cur], 2)
            # Savings can't dip below zero on the chart: if the in-progress
            # month's banked-so-far is negative (only outflows so far — no
            # paycheck/transfers yet), floor the solid bar at 0 and fold the
            # shortfall into the projected remainder, leaving the month's
            # projected total unchanged (no downward solid spike).
            if excl_proj[cur] < 0:
                excl_rem[cur] = round(excl_rem[cur] + excl_proj[cur], 2)
                excl_proj[cur] = 0.0

        # Fully projected future months → translucent bars.
        for m in range(last + 1, proj_last + 1):
            pays = proj_pay[m - 1]
            wt = (pays / all_pays) if all_pays else (1 / month_units)
            excl_proj[m - 1] = round(rem_excl * wt, 2)
            k401_proj[m - 1] = round(rem_k401 * wt, 2)
            actual_proj[m - 1] = round(rent_monthly + biweekly_deposit * pays
                                       + rem_other / month_units, 2)
            expected_proj[m - 1] = round(biweekly_deposit * pays, 2)
            paychecks_proj[m - 1] = proj_pay[m - 1]

    # January's rent was prepaid the prior December, which falls outside a
    # Jan–Dec data window — so January's bar has no rent transfer in it. Assume
    # it was paid in full: add one month's rent to January and flag that slice
    # as assumed so it renders translucent (same "projected rent" styling used
    # for future/in-progress months), rather than being passed off as actual
    # captured spend. Driven by res.assumed_rent so the chart and the YTD
    # spending total stay in lockstep (same guard logic in compute()).
    assumed_rent_month = -1
    if res.assumed_rent:
        actual_proj[0] = round(actual_proj[0] + res.assumed_rent, 2)
        assumed_rent_month = 0

    data = {
        "labels": MONTHS,
        "excl": monthly["excl"],
        "k401": monthly["k401"],
        "incl": monthly["incl"],
        "excl_proj": excl_proj,
        "k401_proj": k401_proj,
        "excl_rem": excl_rem,
        "k401_rem": k401_rem,
        "total_excl": round(res.savings_excl_401k, 2),
        "total_incl": round(res.savings_incl_401k, 2),
        "last_month": last,
        "proj_last": proj_last,
        "cur_partial": cur if cur_frac > 0 else -1,
        "last_month_label": MONTHS[last-1] if last else "",
    }
    spend = {
        "actual": monthly["actual"],
        "expected": monthly["expected"],
        "paychecks": monthly["paychecks"],
        "actual_proj": actual_proj,
        "expected_proj": expected_proj,
        "paychecks_proj": paychecks_proj,
        "rent_rem": rent_rem,
        "other_rem": other_rem,
        "assumed_rent_month": assumed_rent_month,
    }

    return {
        "year": year,
        "label": label or str(year),
        "historical": historical,
        "ytd_heading": ytd_heading,
        "proj_heading": f"Projected to Dec 31, {year}",
        "sav_excl": _dc(res.savings_excl_401k),
        "sav_incl": _dc(res.savings_incl_401k),
        "spending": _dc(res.spending + res.assumed_rent),
        "proj_sav": "~" + _d(res.proj_savings_incl_401k),
        "proj_spend": "~" + _d(res.proj_spending),
        "biweek": f"{biweekly_deposit:.0f}",
        "rent": rent_monthly,
        "data": data,
        "spend": spend,
    }


def build_primitives(
    rows: list[Row],
    as_of: date,
    rent_default: float,
    net_venmo: bool,
    net_gambling: bool,
    td_as_savings: bool,
    seasonality: list[YearSeasonality],
    historical: bool,
) -> dict:
    """Per-panel primitives for client-side recompute.

    Everything here is *independent* of the four adjustable knobs (rent,
    biweekly deposit, 401k, bonus threshold) given the fixed venmo/gambling/TD
    conventions — except the raw SPEND_MAIN and PAYROLL rows, which the JS needs
    to redo the rent-shift and the bonus classification when a knob changes.
    The JS mirrors compute()/compute_monthly()/build_panel() from these."""
    year = as_of.year
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    months_ytd = ((as_of - year_start).days + 1) / 30.4375
    months_remaining = (year_end - as_of).days / 30.4375

    days_jm = days_jd = 0
    d = as_of + timedelta(days=1)
    while d <= year_end:
        if d.month <= 5:
            days_jm += 1
        else:
            days_jd += 1
        d += timedelta(days=1)
    mo_jm_remain = days_jm / 30.4375
    mo_jd_remain = days_jd / 30.4375

    has_seasonality = bool(seasonality)
    seasonal_factor = (sum(s.seasonal_factor for s in seasonality) / len(seasonality)
                       if seasonality else 1.0)

    main_accum = [0.0] * 12
    rws = [0.0] * 12
    td_x = [0.0] * 12
    direct = [0.0] * 12
    gamb = [0.0] * 12
    venmo = [0.0] * 12
    rentd = [0.0] * 12
    spendMain = []
    payroll = []
    for r in rows:
        if r.date < year_start or r.date > as_of:
            continue
        i = r.date.month - 1
        main_accum[i] += r.amount
        if r.cat in ("INVEST_WEALTHFRONT", "INVEST_ROBINHOOD", "SAVINGS_XFER"):
            rws[i] += -r.amount
        elif r.cat == "TD_XFER":
            td_x[i] += -r.amount
        elif r.cat == "SPEND_MAIN":
            spendMain.append({"m": r.date.month, "day": r.date.day,
                              "amt": round(-r.amount, 2)})
        elif r.cat == "DIRECT_EXP":
            direct[i] += -r.amount
        elif r.cat == "GAMBLING_OUT":
            gamb[i] += -r.amount
        elif r.cat == "VENMO_OUT":
            venmo[i] += -r.amount
        elif r.cat == "RENT":
            rentd[i] += -r.amount
        if r.cat == "PAYROLL":
            payroll.append({"m": r.date.month, "day": r.date.day,
                            "amt": round(r.amount, 2)})

    excl = [0.0] * 12
    actual_fixed = [0.0] * 12
    for i in range(12):
        td_sav = td_x[i] if td_as_savings else 0.0
        excl[i] = round(main_accum[i] + rws[i] + td_sav, 2)
        venmo_spend = 0.0 if net_venmo else venmo[i]
        gamb_spend = 0.0 if net_gambling else gamb[i]
        td_spend = 0.0 if td_as_savings else td_x[i]
        actual_fixed[i] = round(direct[i] + rentd[i] + venmo_spend
                                + gamb_spend + td_spend, 2)
    td_net = round(sum(td_x), 2) if td_as_savings else 0.0

    last = max((r.date.month for r in rows if year_start <= r.date <= as_of),
               default=0)

    return {
        "year": year,
        "label": str(year),
        "historical": historical,
        "rent_locked": historical,     # historical panels keep their detected rent
        "rent_default": round(rent_default, 2),
        "as_of": [as_of.year, as_of.month, as_of.day],
        "months_ytd": months_ytd,
        "months_remaining": months_remaining,
        "mo_jm_remain": mo_jm_remain,
        "mo_jd_remain": mo_jd_remain,
        "seasonal_factor": seasonal_factor,
        "has_seasonality": has_seasonality,
        "last_month": last,
        "excl": excl,
        "actual_fixed": actual_fixed,
        "td_net": td_net,
        "spendMain": spendMain,
        "payroll": payroll,
    }


def render_html(panels: list[dict], settings: dict | None = None,
                primitives: list[dict] | None = None,
                needs_onboarding: bool = False) -> str:
    subs = {
        "%%YEAR%%": str(panels[0]["year"]),
        "%%PANELS_JSON%%": json.dumps(panels),
        "%%SETTINGS_JSON%%": json.dumps(settings or {}),
        "%%PRIMITIVES_JSON%%": json.dumps(primitives or []),
        "%%NEEDS_ONBOARDING%%": "true" if needs_onboarding else "false",
    }
    md_path = os.path.join(app_paths.resource_dir(), "market_data.json")
    try:
        with open(md_path) as f:
            subs["%%MARKET_DATA_JSON%%"] = f.read().strip() or "{}"
    except OSError:
        subs["%%MARKET_DATA_JSON%%"] = "{}"
    html = HTML_TEMPLATE
    for tok, val in subs.items():
        html = html.replace(tok, val)
    return html


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", help="Path to transactions CSV")
    p.add_argument("--as-of", type=date.fromisoformat,
                   help="YTD cutoff date (default: today's date)")
    p.add_argument("--k401-annual", type=float, default=None,
                   help="Annual 401k contribution target "
                        "(default: from finance_config.json)")
    p.add_argument("--biweekly-deposit", type=float, default=None,
                   help="Biweekly direct deposit into the spending account "
                        "(default: from finance_config.json)")
    p.add_argument("--rent", type=float, default=None,
                   help="Current monthly rent (default: from finance_config.json). "
                        "Used to normalize non-rent spending for seasonal projection.")
    p.add_argument("--bonus-threshold", type=float, default=None,
                   help="Paychecks at/above this are treated as one-time bonuses "
                        "(default: from finance_config.json)")
    p.add_argument("--prior-years-dir", default=DEFAULT_PRIOR_YEARS_DIR,
                   help=f"Directory of historical CSVs for seasonal lift "
                        f"(default: {DEFAULT_PRIOR_YEARS_DIR})")
    p.add_argument("--no-seasonal", action="store_true",
                   help="Disable seasonal adjustment; use flat run-rate only.")
    p.add_argument("--html", metavar="PATH", default=None,
                   help="Also write the monthly savings/spending dashboard "
                        "HTML to PATH (rebuilt from this run's data).")

    venmo = p.add_mutually_exclusive_group()
    venmo.add_argument("--net-venmo", dest="net_venmo", action="store_true",
                       default=True, help="Net Venmo (default)")
    venmo.add_argument("--gross-venmo", dest="net_venmo", action="store_false",
                       help="Count Venmo OUT as spending in full")

    gamb = p.add_mutually_exclusive_group()
    gamb.add_argument("--net-gambling", dest="net_gambling", action="store_true",
                      default=False, help="Net gambling (off by default)")
    gamb.add_argument("--gross-gambling", dest="net_gambling", action="store_false",
                      help="Count gambling OUT as spending (default)")

    td = p.add_mutually_exclusive_group()
    td.add_argument("--td-as-savings", dest="td_as_savings", action="store_true",
                    default=True, help="Treat TD transfers as savings (default)")
    td.add_argument("--td-as-spending", dest="td_as_savings", action="store_false",
                    help="Treat TD transfers as spending")

    args = p.parse_args(argv)

    # Resolve personal baseline figures: an explicit CLI flag always wins;
    # otherwise use the machine-local config. When the config is missing or
    # incomplete (fresh clone) we do NOT prompt in the terminal — the dashboard
    # collects the values through its first-run onboarding UI and saves them back
    # to finance_config.json via the local bridge. In that case we still build
    # the dashboard (with placeholder zeros the UI overrides) but mark it for
    # onboarding; the client-side recompute fills in real numbers on submit.
    global BONUS_THRESHOLD, GROUP_BUCKET_MAP
    cfg = load_local_config()

    # User's group→bucket mapping (from the ingest mapping screen), applied by
    # load()/apply_group_bucket() to sort each payee group into spending /
    # savings / income. Keys are group_key()s; values are the bucket names.
    gb = cfg.get("group_buckets", {})
    GROUP_BUCKET_MAP = {str(k): str(v) for k, v in gb.items()} if isinstance(gb, dict) else {}

    rent = args.rent if args.rent is not None else cfg.get("rent")
    biweekly = (args.biweekly_deposit if args.biweekly_deposit is not None
                else cfg.get("biweekly_deposit"))
    k401 = args.k401_annual if args.k401_annual is not None else cfg.get("k401_annual")
    receive_bonuses = cfg.get("receive_bonuses")
    bonus = (args.bonus_threshold if args.bonus_threshold is not None
             else cfg.get("bonus_threshold"))

    # Bonus config is "complete" if a threshold is set, or the user has said they
    # don't receive bonuses at all.
    bonus_configured = bonus is not None or receive_bonuses is False
    needs_onboarding = (rent is None or biweekly is None or k401 is None
                        or not bonus_configured)

    if needs_onboarding and not args.html:
        print(
            "No personal baseline config found. Run via /finproject (which builds "
            "the dashboard) and complete the one-time setup in the UI, or pass "
            "--rent / --biweekly-deposit / --k401-annual / --bonus-threshold.",
            file=sys.stderr)
        return 2

    # Values used for computation (placeholders during onboarding).
    args.rent = rent if rent is not None else 0.0
    args.biweekly_deposit = biweekly if biweekly is not None else 0.0
    args.k401_annual = k401 if k401 is not None else 0.0
    args.receive_bonuses = receive_bonuses if receive_bonuses is not None else True
    args.bonus_threshold = bonus
    args.needs_onboarding = needs_onboarding

    # compute()/compute_monthly()/_period_spending() read BONUS_THRESHOLD as a
    # module global. When the user doesn't take bonuses (or hasn't onboarded yet)
    # nothing is a bonus, so use +inf.
    BONUS_THRESHOLD = bonus if (args.receive_bonuses and bonus is not None) else math.inf

    rows = load(args.csv)
    if not rows:
        print("No rows loaded.", file=sys.stderr)
        return 1

    as_of = args.as_of or date.today()

    seasonality = []
    if not args.no_seasonal:
        seasonality = load_historical(args.prior_years_dir,
                                       args.biweekly_deposit)
        # Don't include the current year if it shows up in prior_years/
        seasonality = [s for s in seasonality if s.year != as_of.year]

    res = compute(
        rows, as_of=as_of,
        k401_annual=args.k401_annual,
        biweekly_deposit=args.biweekly_deposit,
        net_venmo=args.net_venmo,
        net_gambling=args.net_gambling,
        td_as_savings=args.td_as_savings,
        rent_monthly=args.rent,
        seasonality=seasonality,
    )
    print(_setup_message() if args.needs_onboarding else render(res))

    if args.html:
        monthly = compute_monthly(
            rows, as_of=as_of,
            k401_annual=args.k401_annual,
            biweekly_deposit=args.biweekly_deposit,
            net_venmo=args.net_venmo,
            net_gambling=args.net_gambling,
            td_as_savings=args.td_as_savings,
            rent_monthly=args.rent,
        )
        panels = [build_panel(res, monthly, args.biweekly_deposit, args.rent,
                              args.k401_annual)]
        primitives = [build_primitives(
            rows, as_of, args.rent, args.net_venmo, args.net_gambling,
            args.td_as_savings, seasonality, historical=False)]

        # Add a tab per prior-year CSV (same data, same format). Each completed
        # year is run with as-of = Dec 31, so its "projection" equals its actual.
        for path in sorted(glob.glob(os.path.join(args.prior_years_dir, "*.csv"))):
            pri_rows = load(path)
            if not pri_rows:
                continue
            for y in sorted(set(r.date.year for r in pri_rows), reverse=True):
                if y == as_of.year:
                    continue  # already covered by the live tab
                y_as_of = date(y, 12, 31)
                y_rent = _detect_year_rent(
                    [r for r in pri_rows if r.date.year == y]) or args.rent
                y_res = compute(
                    pri_rows, as_of=y_as_of,
                    k401_annual=args.k401_annual,
                    biweekly_deposit=args.biweekly_deposit,
                    net_venmo=args.net_venmo,
                    net_gambling=args.net_gambling,
                    td_as_savings=args.td_as_savings,
                    rent_monthly=y_rent,
                    seasonality=[],
                )
                y_monthly = compute_monthly(
                    pri_rows, as_of=y_as_of,
                    k401_annual=args.k401_annual,
                    biweekly_deposit=args.biweekly_deposit,
                    net_venmo=args.net_venmo,
                    net_gambling=args.net_gambling,
                    td_as_savings=args.td_as_savings,
                    rent_monthly=y_rent,
                )
                panels.append(build_panel(y_res, y_monthly,
                                          args.biweekly_deposit, y_rent,
                                          args.k401_annual, historical=True))
                primitives.append(build_primitives(
                    pri_rows, y_as_of, y_rent, args.net_venmo, args.net_gambling,
                    args.td_as_savings, [], historical=True))

        settings = {
            "rent": args.rent,
            "biweekly_deposit": args.biweekly_deposit,
            "k401_annual": args.k401_annual,
            "receive_bonuses": args.receive_bonuses,
            "bonus_threshold": args.bonus_threshold if args.bonus_threshold is not None else 0,
        }
        with open(args.html, "w") as f:
            f.write(render_html(panels, settings, primitives,
                                needs_onboarding=args.needs_onboarding))
        print(f"\n_Dashboard HTML written to {args.html}_", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
