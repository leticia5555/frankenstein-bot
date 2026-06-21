#!/usr/bin/env python3
"""
halloween_test.py — does the "Sell in May" / Halloween calendar effect hold?

The folklore rule: be LONG the market Nov–Apr ("winter"), be OUT May–Oct
("summer"). This tests it honestly on real index data.

Same discipline as the rest of the toolkit: don't just compute one number over
all history. We split the years chronologically (first 70% / last 30%) and check
whether any winter-beats-summer edge HELD or FADED over time — a seasonal effect
that worked in the 1990s may be arbitraged away now.

Definitions (standard Halloween-effect convention, anchored on month-end closes):
  - Summer[Y] return = close(end of Oct, Y)  / close(end of Apr, Y)   - 1
  - Winter[Y] return = close(end of Apr, Y+1) / close(end of Oct, Y)  - 1
Entry/exit use the last trading day of the anchor month.

What it reports:
  - avg return of the Nov–Apr window vs the May–Oct window
  - % of years Nov–Apr beat May–Oct
  - compounded growth of "invested winter-only" (cash in summer) vs buy-and-hold,
    over the identical span
  - 70/30 split for stability
  - plain-language verdict: real edge, faded, or noise

Usage:
    python3 halloween_test.py SPY.csv

Pure historical analysis. No trading.
"""

import argparse
import sys

# Reuse the robust loader (Close/Last, $-stripping, oldest-first ordering).
from backtest_stock import load_csv, _date_key

IN_SAMPLE_FRACTION = 0.70
ENTRY_MONTH = 10  # October — end-of-Oct close starts winter / ends summer
EXIT_MONTH = 4    # April   — end-of-Apr close ends winter / starts summer


# ---------------------------------------------------------------------------
# Month-end closes
# ---------------------------------------------------------------------------

def month_end_closes(dates, closes):
    """Map (year, month) -> closing price on the LAST trading day of that month.

    Rows are oldest-first, so the last close we see for a given (year, month)
    is that month's final trading day.
    """
    eom = {}
    for d, c in zip(dates, closes):
        k = _date_key(d)
        if k is None:
            continue
        eom[(k.year, k.month)] = c
    return eom


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def compound(returns):
    """Compounded growth multiple from a list of period returns (1.0 = flat)."""
    g = 1.0
    for r in returns:
        g *= (1.0 + r)
    return g


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_seasons(eom):
    """Return a sorted list of dicts, one per year that has BOTH a summer and a
    winter return available, each with the two returns."""
    years = sorted({y for (y, m) in eom})
    seasons = []
    for y in years:
        apr_y = eom.get((y, EXIT_MONTH))
        oct_y = eom.get((y, ENTRY_MONTH))
        apr_next = eom.get((y + 1, EXIT_MONTH))
        if apr_y is None or oct_y is None or apr_next is None:
            continue  # need all three anchors for a complete paired year
        seasons.append({
            "year": y,
            "summer": oct_y / apr_y - 1,       # May–Oct of year Y
            "winter": apr_next / oct_y - 1,    # Nov Y – Apr Y+1
        })
    return seasons


def report_split(title, rows):
    n = len(rows)
    if n == 0:
        print(f"  {title}: (no complete years)")
        return
    aw = avg([r["winter"] for r in rows])
    as_ = avg([r["summer"] for r in rows])
    beat = sum(1 for r in rows if r["winter"] > r["summer"]) / n
    print(f"  {title}  ({rows[0]['year']}-{rows[-1]['year']}, {n} years)")
    print(f"    avg Nov-Apr (winter) : {aw * 100:+7.2f}%")
    print(f"    avg May-Oct (summer) : {as_ * 100:+7.2f}%")
    print(f"    winter - summer edge : {(aw - as_) * 100:+7.2f} pts")
    print(f"    Nov-Apr beat May-Oct : {beat * 100:5.1f}% of years ({sum(1 for r in rows if r['winter'] > r['summer'])}/{n})")


def main():
    parser = argparse.ArgumentParser(
        description="Test the Halloween / Sell-in-May seasonal effect."
    )
    parser.add_argument("csv", help="Index CSV (e.g. SPY.csv)")
    parser.add_argument("--stake", type=float, default=10000.0,
                        help="Starting capital for the growth comparison "
                             "(default: 10000).")
    args = parser.parse_args()

    dates, closes = load_csv(args.csv)
    eom = month_end_closes(dates, closes)
    seasons = build_seasons(eom)

    if len(seasons) < 4:
        sys.exit(
            f"Not enough complete years to test: only {len(seasons)} year(s) "
            f"with both a full summer and winter window in {args.csv}. "
            f"Need several years of data spanning Oct–Apr."
        )

    # Chronological 70/30 split on the years.
    split = int(len(seasons) * IN_SAMPLE_FRACTION)
    is_rows = seasons[:split]
    oos_rows = seasons[split:]

    # Overall stats.
    aw_all = avg([r["winter"] for r in seasons])
    as_all = avg([r["summer"] for r in seasons])
    beat_all = sum(1 for r in seasons if r["winter"] > r["summer"]) / len(seasons)

    # Compounded growth over the IDENTICAL span: alternating summer, winter
    # segments in chronological order. Buy-and-hold rides every segment;
    # winter-only sits in cash (x1) during summers.
    segments = []
    for r in seasons:
        segments.append(("summer", r["summer"]))
        segments.append(("winter", r["winter"]))
    bh_growth = compound([r for _, r in segments])
    winter_only_growth = compound([r for kind, r in segments if kind == "winter"])
    summer_only_growth = compound([r for kind, r in segments if kind == "summer"])

    span_lo = seasons[0]["year"]
    span_hi = seasons[-1]["year"] + 1

    # ----- Report -----
    print("=" * 66)
    print("  HALLOWEEN / SELL-IN-MAY TEST")
    print("=" * 66)
    print(f"  data            : {args.csv}  ({len(closes):,} days, "
          f"{dates[0]} -> {dates[-1]})")
    print(f"  complete years  : {len(seasons)}  (summer+winter pairs, "
          f"{span_lo}-{span_hi})")
    print(f"  split           : {len(is_rows)} in-sample / {len(oos_rows)} out-of-sample")
    print("-" * 66)
    print("  ALL YEARS")
    print(f"    avg Nov-Apr (winter) : {aw_all * 100:+7.2f}%")
    print(f"    avg May-Oct (summer) : {as_all * 100:+7.2f}%")
    print(f"    winter - summer edge : {(aw_all - as_all) * 100:+7.2f} pts")
    print(f"    Nov-Apr beat May-Oct : {beat_all * 100:5.1f}% of years")
    print("-" * 66)
    report_split("IN-SAMPLE (first 70%)", is_rows)
    print()
    report_split("OUT-OF-SAMPLE (last 30%)", oos_rows)
    print("-" * 66)
    print(f"  COMPOUNDED GROWTH  (over {span_lo}-{span_hi}, ${args.stake:,.0f} start)")
    print(f"    buy & hold         : x{bh_growth:5.2f}  -> ${args.stake * bh_growth:>12,.0f}")
    print(f"    winter-only (cash in summer): x{winter_only_growth:5.2f}  "
          f"-> ${args.stake * winter_only_growth:>12,.0f}")
    print(f"    summer-only (cash in winter): x{summer_only_growth:5.2f}  "
          f"-> ${args.stake * summer_only_growth:>12,.0f}")
    print("-" * 66)

    # ----- Verdict -----
    is_edge = avg([r["winter"] for r in is_rows]) - avg([r["summer"] for r in is_rows])
    oos_edge = (avg([r["winter"] for r in oos_rows]) -
                avg([r["summer"] for r in oos_rows])) if oos_rows else 0.0

    if oos_edge >= 0.01 and is_edge > 0:
        label = "REAL EDGE (held)"
        why = (f"Nov-Apr beat May-Oct both in-sample ({is_edge * 100:+.1f} pts) "
               f"and out-of-sample ({oos_edge * 100:+.1f} pts) — the seasonal "
               f"edge persisted.")
    elif is_edge >= 0.02 and oos_edge < 0.01:
        label = "FADED"
        why = (f"The edge was there in-sample ({is_edge * 100:+.1f} pts) but "
               f"shrank/flipped out-of-sample ({oos_edge * 100:+.1f} pts) — "
               f"looks arbitraged away.")
    else:
        label = "NOISE / WEAK"
        why = (f"No consistent edge (in-sample {is_edge * 100:+.1f} pts, "
               f"out-of-sample {oos_edge * 100:+.1f} pts) — not reliably "
               f"distinguishable from noise.")
    print(f"  VERDICT: {label}")
    print(f"  {why}")

    # Honesty on sample size — seasonal tests are inherently low-N.
    print(f"  NOTE: only {len(seasons)} years total "
          f"({len(oos_rows)} out-of-sample) — seasonal effects are LOW-SAMPLE "
          f"by nature; treat as indicative, not conclusive.")
    print("=" * 66)

    # One-line plain-English summary.
    direction = ("beat" if aw_all > as_all else "trailed")
    print(f"\nSummary: over {len(seasons)} years, Nov-Apr averaged "
          f"{aw_all * 100:+.1f}% vs May-Oct {as_all * 100:+.1f}% (winter "
          f"{direction} summer, {beat_all * 100:.0f}% of years). Verdict: {label}.")


if __name__ == "__main__":
    main()
