#!/usr/bin/env python3
"""
correlation.py — how tightly do two assets move together, and does it last?

Question this answers: e.g. how tightly does AAPL move with the overall tech
market (QQQ), and is that bond STABLE or does it DRIFT over time?

Same honesty discipline as the rest of the toolkit (validate.py etc.): we don't
just compute one correlation number over all history and call it a day. A
relationship that held in 2017 may have decayed by 2025. So we split the
aligned history chronologically (first 70% / last 30%) and report the
correlation IN-SAMPLE and OUT-OF-SAMPLE separately — if they differ a lot, the
bond changed.

Method:
  - Load both CSVs (reusing backtest_stock.load_csv: handles 'Close/Last',
    '$' prices, and newest-first Nasdaq ordering).
  - Align the two series by DATE (intersection — only days both traded).
  - Compute daily returns r_t = close_t / close_(t-1) - 1 on the aligned dates.
  - Pearson correlation of the two return series, overall + per split.

Usage:
    python3 correlation.py AAPL.csv QQQ.csv
    python3 correlation.py AAPL.csv QQQ.csv --name-a AAPL --name-b QQQ

Pure historical analysis. No trading.
"""

import argparse
import os
import sys

# Reuse the robust CSV loader (Close/Last, $-stripping, oldest-first ordering).
from backtest_stock import load_csv

IN_SAMPLE_FRACTION = 0.70  # same 70/30 boundary as the rest of the toolkit

# Minimum aligned return observations before the result is trustworthy.
MIN_OBS = 30


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def pearson(xs, ys):
    """Pearson correlation coefficient of two equal-length sequences.

    Returns None if it's undefined (fewer than 2 points, or a series with zero
    variance — e.g. a flat line, where correlation has no meaning).
    """
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def daily_returns(closes):
    """Convert a price series to simple daily returns (one shorter)."""
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

def align_by_date(dates_a, closes_a, dates_b, closes_b):
    """Return (common_dates, closes_a_aligned, closes_b_aligned).

    Keeps only dates present in BOTH series, in chronological order. Both
    loaders already return oldest-first, but we sort the intersection by real
    date to be safe.
    """
    from backtest_stock import _date_key  # reuse the date parser

    map_a = dict(zip(dates_a, closes_a))
    map_b = dict(zip(dates_b, closes_b))
    common = set(map_a) & set(map_b)
    if not common:
        return [], [], []

    # Sort by parsed date when possible, else lexically.
    def key(d):
        k = _date_key(d)
        return (0, k) if k is not None else (1, d)

    ordered = sorted(common, key=key)
    ca = [map_a[d] for d in ordered]
    cb = [map_b[d] for d in ordered]
    return ordered, ca, cb


# ---------------------------------------------------------------------------
# Plain-language helpers
# ---------------------------------------------------------------------------

def strength_label(r):
    """Plain-language strength of a correlation coefficient."""
    if r is None:
        return "undefined"
    a = abs(r)
    sign = "positive" if r >= 0 else "negative"
    if a >= 0.8:
        return f"very strong {sign}"
    if a >= 0.6:
        return f"strong {sign}"
    if a >= 0.4:
        return f"moderate {sign}"
    if a >= 0.2:
        return f"weak {sign}"
    return f"very weak / no {sign}"


def stability_verdict(r_is, r_oos):
    """Compare in-sample vs out-of-sample correlation -> stable or drifted."""
    if r_is is None or r_oos is None:
        return "UNKNOWN", "Correlation undefined in one split (flat series?)."
    drift = abs(r_oos - r_is)
    if drift < 0.10:
        return ("STABLE",
                f"Correlation barely moved ({r_is:+.2f} -> {r_oos:+.2f}, "
                f"change {drift:.2f}); the relationship held.")
    if drift < 0.20:
        return ("MILD DRIFT",
                f"Correlation shifted somewhat ({r_is:+.2f} -> {r_oos:+.2f}, "
                f"change {drift:.2f}); watch it.")
    return ("CHANGED",
            f"Correlation moved a lot ({r_is:+.2f} -> {r_oos:+.2f}, "
            f"change {drift:.2f}); the relationship is not stable over time.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Correlation between two assets' daily returns, "
                    "in-sample vs out-of-sample."
    )
    parser.add_argument("csv_a", help="First asset CSV (e.g. AAPL.csv)")
    parser.add_argument("csv_b", help="Second asset CSV (e.g. QQQ.csv)")
    parser.add_argument("--name-a", default=None, help="Label for asset A.")
    parser.add_argument("--name-b", default=None, help="Label for asset B.")
    args = parser.parse_args()

    name_a = args.name_a or os.path.splitext(os.path.basename(args.csv_a))[0]
    name_b = args.name_b or os.path.splitext(os.path.basename(args.csv_b))[0]

    dates_a, closes_a = load_csv(args.csv_a)
    dates_b, closes_b = load_csv(args.csv_b)

    # Align by date.
    common, ca, cb = align_by_date(dates_a, closes_a, dates_b, closes_b)
    overlap = len(common)
    if overlap < MIN_OBS + 1:  # +1 because returns lose one row
        sys.exit(
            f"Not enough overlapping dates to measure correlation: only "
            f"{overlap} common trading day(s) between {name_a} and {name_b} "
            f"(need > {MIN_OBS + 1}). "
            f"{name_a}: {len(dates_a)} rows, {name_b}: {len(dates_b)} rows. "
            f"Check the date ranges actually overlap."
        )

    # Daily returns on the aligned series (these line up day-for-day).
    ra = daily_returns(ca)
    rb = daily_returns(cb)
    ret_dates = common[1:]  # each return is labeled by its end date
    n = len(ra)

    # Chronological 70/30 split of the return observations.
    split = int(n * IN_SAMPLE_FRACTION)
    r_overall = pearson(ra, rb)
    r_is = pearson(ra[:split], rb[:split])
    r_oos = pearson(ra[split:], rb[split:])

    # Report.
    print("=" * 66)
    print("  CORRELATION CHECK  (daily returns, decay-tested)")
    print("=" * 66)
    print(f"  asset A         : {name_a}  ({args.csv_a})")
    print(f"  asset B         : {name_b}  ({args.csv_b})")
    print(f"  overlap         : {overlap:,} common trading days "
          f"({ret_dates[0]} -> {ret_dates[-1]})")
    print(f"  return obs      : {n:,}")
    print(f"  split           : {split:,} in-sample / {n - split:,} out-of-sample")
    print("-" * 66)
    print(f"  OVERALL corr    : {r_overall:+.3f}  ({strength_label(r_overall)})")
    print("-" * 66)
    print(f"  IN-SAMPLE  (first 70%): {r_is:+.3f}  ({strength_label(r_is)})")
    print(f"     {ret_dates[0]} -> {ret_dates[split - 1]}")
    print(f"  OUT-OF-SAMPLE (last 30%): {r_oos:+.3f}  ({strength_label(r_oos)})")
    print(f"     {ret_dates[split]} -> {ret_dates[-1]}")
    print("-" * 66)
    label, why = stability_verdict(r_is, r_oos)
    print(f"  VERDICT: {label}")
    print(f"  {why}")
    print("=" * 66)

    # One-line plain-English summary.
    print(f"\nSummary: {name_a} and {name_b} have a {strength_label(r_overall)} "
          f"daily-return correlation ({r_overall:+.2f} overall). "
          f"Across the 70/30 split it went {r_is:+.2f} -> {r_oos:+.2f} "
          f"({label}). {why}")


if __name__ == "__main__":
    main()
