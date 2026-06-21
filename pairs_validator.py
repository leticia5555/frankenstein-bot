#!/usr/bin/env python3
"""
pairs_validator.py — mean-reversion / pairs cointegration validator.

Same judge, new test type. Asks not "does a signal predict?" but
"is this spread ACTUALLY cointegrated, or does the relationship fall
apart out-of-sample?"

Three gates, in order. A pair must pass all three to earn VENTAJA REAL.
The verdict NAMES the gate it failed (matches final_verdict.py surface).

    G1  Engle-Granger in-sample : is there a relationship at all?
        regress A on B, ADF on the residual. residual must be stationary.
    G2  OU half-life + K        : does it revert fast enough to trade,
        and did you overfit by testing too many pairs? (Bonferroni on K)
    G3  OOS cointegration       : re-test EG-ADF on the 30% holdout.
        the relationship must STILL be stationary on data you never fit.
        ^ this is the kill shot. never just re-run the trade in-sample.

Verdict tiers (reused from the engine):
    RUIDO         fail G1 (no relationship in-sample)
    MARGINAL      fail G2 (too slow, or fails Bonferroni across K pairs)
    SOBREAJUSTADO fail G3 (in-sample only; decoheres out-of-sample)
    INSUFICIENTE  no G3 data / too few points to split
    VENTAJA REAL  all three pass
"""

import argparse
import sys

# ----- config knobs (tune later, don't bikeshed tonight) --------------------
OOS_FRACTION = 0.30          # holdout locked away BEFORE any gate runs
ADF_PVALUE_MAX = 0.05        # residual stationarity threshold
HALFLIFE_MAX_DAYS = 60       # OU half-life ceiling; slower = "demasiado lento"
K_BASE_ALPHA = 0.05          # Bonferroni base; threshold = alpha / K


# ----- the split is the FIRST thing that happens. nothing peeks at OOS. -----
def chronological_split(series_a, series_b, oos_fraction=OOS_FRACTION):
    """
    Chronological 70/30. Returns (in_sample, out_of_sample) as tuples of
    (a, b). MUST be called before any gate touches the data. The holdout
    is not looked at, fit on, or summarized until Gate 3.
    """
    n = len(series_a)
    if n != len(series_b):
        raise ValueError("series A and B must be the same length")
    cut = int(n * (1 - oos_fraction))
    if cut < 1 or (n - cut) < 1:
        # not enough data to honestly split -> caller emits INSUFICIENTE
        return None, None
    in_s = (series_a[:cut], series_b[:cut])
    oos = (series_a[cut:], series_b[cut:])
    return in_s, oos


# ----- GATE 1 ---------------------------------------------------------------
def gate1_engle_granger(a_in, b_in):
    """
    In-sample Engle-Granger.
    1. OLS regress a on b -> hedge ratio (beta) and residual = a - beta*b
    2. ADF test on residual.
    Returns dict: {beta, adf_pvalue, stationary: bool}
    Pass when adf_pvalue <= ADF_PVALUE_MAX.
    """
    raise NotImplementedError("Gate 1: OLS hedge ratio + ADF on residual")


# ----- GATE 2 ---------------------------------------------------------------
def gate2_halflife_and_k(residual_in, k_this_hypothesis):
    """
    OU half-life on the in-sample spread, plus the multiple-testing gate.
    half-life via AR(1) on the spread: d(spread) = lambda * spread_lag + e,
        half-life = -ln(2) / ln(1 + lambda)   (guard lambda in (-1, 0))
    Bonferroni: effective threshold = K_BASE_ALPHA / k_this_hypothesis.
    Returns dict: {half_life_days, too_slow: bool, k_threshold, passes_k: bool}
    K counts distinct pairs tested under the same hypothesis (GOOGL/AMZN,
    GOOGL/MSFT, ... are all "the same idea" being shopped).
    """
    raise NotImplementedError("Gate 2: OU half-life + Bonferroni on K")


# ----- GATE 3 (the kill shot) ----------------------------------------------
def gate3_oos_cointegration(a_oos, b_oos, beta_in):
    """
    Re-test cointegration on the holdout. CRITICAL: use the IN-SAMPLE beta
    to form the OOS residual (do NOT refit beta on OOS — that would be
    fitting to the holdout). Then ADF on the OOS residual.
    The relationship must STILL be stationary on data never fit.
    Returns dict: {adf_pvalue, still_cointegrated: bool}
    """
    raise NotImplementedError("Gate 3: ADF on OOS residual using in-sample beta")


# ----- verdict assembly -----------------------------------------------------
def decide(g1, g2, g3, has_oos):
    """Chain the gates into one Spanish verdict that names the failed gate."""
    if g1 is None or not g1["stationary"]:
        return "RUIDO", "sin relacion en la muestra (falla G1: cointegracion)"
    if not (g2["passes_k"] and not g2["too_slow"]):
        why = "demasiado lento" if g2["too_slow"] else "no supera Bonferroni (K)"
        return "MARGINAL", f"falla G2: {why}"
    if not has_oos:
        return "INSUFICIENTE", "sin datos out-of-sample para confirmar"
    if not g3["still_cointegrated"]:
        return "SOBREAJUSTADO", "se rompe fuera de muestra (falla G3)"
    return "VENTAJA REAL", "confirmado out-of-sample: cointegracion real"


# ----- CLI ------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="Pairs / mean-reversion validator")
    p.add_argument("--data", required=True, help="CSV with both legs")
    p.add_argument("--a", required=True, help="column name for leg A")
    p.add_argument("--b", required=True, help="column name for leg B")
    p.add_argument("--user", required=True)
    p.add_argument("--hypothesis", required=True, help="e.g. pairs_meanrev")
    p.add_argument("--db", default="ktrack.db", help=":memory: for throwaway")
    args = p.parse_args(argv)

    # TODO: load CSV -> series_a, series_b  (reuse loader from validate.py)
    series_a, series_b = [], []  # placeholder

    in_s, oos = chronological_split(series_a, series_b)
    if in_s is None:
        print("INSUFICIENTE: muy pocos datos para dividir 70/30")
        return 2

    # TODO: K lookup/increment via k_tracker on (hypothesis, fingerprint)
    #   fingerprint = f"{args.a}~{args.b} :: pairs_meanrev"
    k_this = 1  # placeholder until k_tracker is wired in

    g1 = gate1_engle_granger(*in_s)
    residual_in = None  # TODO: a_in - g1["beta"] * b_in
    g2 = gate2_halflife_and_k(residual_in, k_this)
    g3 = gate3_oos_cointegration(oos[0], oos[1], g1["beta"]) if oos else None

    verdict, reason = decide(g1, g2, g3, has_oos=bool(oos))
    print(f"FINAL: {verdict}\n{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
