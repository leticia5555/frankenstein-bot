#!/usr/bin/env python3
"""
validate.py — a "validation gate" for trading signals.

Step 1 of a bigger edge-validation engine. The job of this script is NOT to
find signals — it's to be a skeptic. You hand it a simple rule (e.g.
"rsi < 30") and it tells you, honestly, whether that rule has a real
predictive edge or whether it's just noise / overfit to the past.

The discipline it enforces:
  1. In-sample vs out-of-sample split. A rule that only "works" on the data
     you tuned it on is worthless. We split chronologically (first 70% =
     in-sample, last 30% = out-of-sample) and report both separately.
  2. Beat the base rate, not 50%. In this dataset "UP" already happens some
     fraction of the time on its own. A signal is only interesting if it
     predicts UP *more often* than UP just happens anyway.
  3. A blunt verdict: REAL EDGE / OVERFIT / NO EDGE.

Convention: a signal is treated as a *bullish* prediction (it predicts the
next outcome will be "UP"). A row "wins" when the signal fires AND the
outcome is UP. Use --predict DN to flip it and test bearish signals instead.

Usage:
    python3 validate.py "rsi < 30"
    python3 validate.py "momentum_3m > 0.5"
    python3 validate.py "rsi < 30 and momentum_3m > 0"
    python3 validate.py "rsi > 70" --predict DN

Keep it simple: standard library only, one file.
"""

import argparse
import csv
import sys

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_FILE = "btc_15m_data_v63.csv"

# The numeric feature columns a rule is allowed to reference. We expose these
# (and nothing else) to the rule expression, so a rule can't reach into
# arbitrary Python.
FEATURE_COLUMNS = [
    "minute", "btc_open", "btc_current", "btc_change_pct",
    "up_price", "dn_price", "price_gap",
    "momentum_1m", "momentum_3m", "rsi",
    "macd_line", "macd_signal", "macd_histogram",
    "ha_trend", "vwap_deviation", "volatility",
    "frondent_signal", "total_cost",
]

# How much the out-of-sample win rate must beat the base rate (in percentage
# points) before we're willing to call it a REAL EDGE. This is a guard against
# tiny, noise-sized "edges". Tune with --edge-threshold.
DEFAULT_EDGE_THRESHOLD = 1.0  # percentage points

# Minimum number of times a signal must fire out-of-sample for us to trust the
# number at all. A 90% win rate over 4 samples means nothing.
DEFAULT_MIN_SAMPLES = 30

IN_SAMPLE_FRACTION = 0.70


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_rows(path):
    """Load the CSV into a list of dicts, coercing feature columns to float.

    Rows whose feature values can't be parsed (blank / malformed) are skipped
    so a few bad lines don't crash the whole run.
    """
    rows = []
    skipped = 0
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = {}
            ok = True
            for col in FEATURE_COLUMNS:
                try:
                    row[col] = float(raw[col])
                except (KeyError, ValueError, TypeError):
                    ok = False
                    break
            if not ok:
                skipped += 1
                continue
            # outcome is the label we're trying to predict ("UP" / "DN").
            row["outcome"] = (raw.get("outcome") or "").strip().upper()
            if row["outcome"] not in ("UP", "DN"):
                skipped += 1
                continue
            rows.append(row)
    return rows, skipped


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------

def compile_rule(rule):
    """Compile the rule string once into a Python code object.

    We evaluate it later against each row with a locked-down namespace
    (no builtins), so the only names available are the feature columns.
    """
    try:
        return compile(rule, "<signal-rule>", "eval")
    except SyntaxError as e:
        sys.exit(f"Could not parse signal rule {rule!r}: {e}")


def signal_fires(code, row):
    """Return True if the signal rule is satisfied for this row."""
    try:
        return bool(eval(code, {"__builtins__": {}}, row))
    except NameError as e:
        # Reference to a column that doesn't exist — tell the user clearly.
        sys.exit(
            f"Signal rule references an unknown column ({e}).\n"
            f"Available columns: {', '.join(FEATURE_COLUMNS)}"
        )
    except Exception as e:
        sys.exit(f"Error evaluating rule on a row: {e}")


# ---------------------------------------------------------------------------
# Core stats
# ---------------------------------------------------------------------------

def evaluate(rows, code, predict):
    """Compute signal stats over a set of rows.

    Returns a dict with:
      base_rate : fraction of rows whose outcome == predict (what you'd get
                  by always guessing `predict`, i.e. random/no-signal).
      fired     : how many rows the signal fired on (sample size).
      wins      : of those, how many had outcome == predict.
      win_rate  : wins / fired.
      edge      : win_rate - base_rate (the only number that really matters).
    """
    total = len(rows)
    base_hits = sum(1 for r in rows if r["outcome"] == predict)
    base_rate = base_hits / total if total else 0.0

    fired = 0
    wins = 0
    for r in rows:
        if signal_fires(code, r):
            fired += 1
            if r["outcome"] == predict:
                wins += 1

    win_rate = wins / fired if fired else 0.0
    return {
        "total": total,
        "base_rate": base_rate,
        "fired": fired,
        "wins": wins,
        "win_rate": win_rate,
        "edge": win_rate - base_rate,
    }


def verdict(is_stats, oos_stats, edge_threshold, min_samples):
    """Turn the in-sample / out-of-sample numbers into a blunt verdict.

    Logic:
      - If out-of-sample never (or barely) fires, we can't judge -> NO EDGE
        (insufficient data).
      - REAL EDGE: out-of-sample win rate beats the out-of-sample base rate
        by at least `edge_threshold` points. This is the gate that matters:
        it held up on data we didn't look at.
      - OVERFIT: it had an edge in-sample but that edge evaporated (or went
        negative) out-of-sample. Classic curve-fitting.
      - NO EDGE: it didn't beat the base rate in-sample either, so there was
        never anything there.
    """
    is_edge_pts = is_stats["edge"] * 100
    oos_edge_pts = oos_stats["edge"] * 100

    if oos_stats["fired"] < min_samples:
        return (
            "NO EDGE",
            f"Out-of-sample sample too small ({oos_stats['fired']} < "
            f"{min_samples}); can't trust the result.",
        )

    if oos_edge_pts >= edge_threshold:
        return (
            "REAL EDGE",
            f"Out-of-sample win rate beats the base rate by "
            f"{oos_edge_pts:+.2f} pts on {oos_stats['fired']} samples.",
        )

    if is_edge_pts >= edge_threshold:
        return (
            "OVERFIT",
            f"Worked in-sample ({is_edge_pts:+.2f} pts) but failed "
            f"out-of-sample ({oos_edge_pts:+.2f} pts).",
        )

    return (
        "NO EDGE",
        f"Never beat the base rate (in-sample {is_edge_pts:+.2f} pts, "
        f"out-of-sample {oos_edge_pts:+.2f} pts).",
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def pct(x):
    return f"{x * 100:6.2f}%"


def print_block(title, s):
    """Pretty-print one split's stats."""
    coverage = (s["fired"] / s["total"] * 100) if s["total"] else 0.0
    print(f"  {title}")
    print(f"    rows in split   : {s['total']:>8,}")
    print(f"    signal fired    : {s['fired']:>8,}  ({coverage:5.2f}% of rows)")
    print(f"    wins            : {s['wins']:>8,}")
    print(f"    win rate        : {pct(s['win_rate'])}")
    print(f"    base rate       : {pct(s['base_rate'])}  (outcome happens anyway)")
    print(f"    edge vs base    : {s['edge'] * 100:+6.2f} pts")


def main():
    parser = argparse.ArgumentParser(
        description="Validation gate: is a trading signal a real edge or overfit?"
    )
    parser.add_argument(
        "rule",
        help='Signal rule, e.g. "rsi < 30" or "momentum_3m > 0.5 and rsi < 40"',
    )
    parser.add_argument(
        "--predict",
        choices=["UP", "DN"],
        default="UP",
        help="Direction the signal predicts (default: UP).",
    )
    parser.add_argument(
        "--data",
        default=DATA_FILE,
        help=f"CSV data file (default: {DATA_FILE}).",
    )
    parser.add_argument(
        "--edge-threshold",
        type=float,
        default=DEFAULT_EDGE_THRESHOLD,
        help="Min out-of-sample edge in pts to call it REAL (default: "
        f"{DEFAULT_EDGE_THRESHOLD}).",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help="Min out-of-sample firings to trust the result (default: "
        f"{DEFAULT_MIN_SAMPLES}).",
    )
    args = parser.parse_args()

    # Load and split (chronologically — the file is already in time order).
    rows, skipped = load_rows(args.data)
    if not rows:
        sys.exit(f"No usable rows loaded from {args.data}.")

    split = int(len(rows) * IN_SAMPLE_FRACTION)
    in_sample = rows[:split]
    out_sample = rows[split:]

    code = compile_rule(args.rule)
    is_stats = evaluate(in_sample, code, args.predict)
    oos_stats = evaluate(out_sample, code, args.predict)
    label, reason = verdict(is_stats, oos_stats, args.edge_threshold, args.min_samples)

    # Report.
    print("=" * 64)
    print("  SIGNAL VALIDATION GATE")
    print("=" * 64)
    print(f"  rule            : {args.rule}")
    print(f"  predicting      : {args.predict}")
    print(f"  data            : {args.data}")
    print(f"  total rows used : {len(rows):,}" + (f"  ({skipped:,} skipped)" if skipped else ""))
    print(f"  split           : {len(in_sample):,} in-sample / {len(out_sample):,} out-of-sample")
    print("-" * 64)
    print_block("IN-SAMPLE  (first 70%)", is_stats)
    print()
    print_block("OUT-OF-SAMPLE  (last 30%)", oos_stats)
    print("-" * 64)
    print(f"  VERDICT: {label}")
    print(f"  {reason}")
    print("=" * 64)


if __name__ == "__main__":
    main()
