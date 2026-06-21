#!/usr/bin/env python3
"""
final_verdict.py — one test in, ONE final verdict out.

Wires the three credibility gates into a single chain so a test can't pass by
sneaking through one layer:

    raw p-value  ->  K-adjusted (Bonferroni, from k_tracker)  ->  OOS-confirmed
                                                                   (70/30, from
                                                                    validate.py)

Final tiers (Spanish):
    VENTAJA REAL   p < 0.05/K  AND confirmed out-of-sample
    MARGINAL       p < 0.05 (raw) but fails the K bar, yet confirmed OOS
    RUIDO          p >= 0.05 (fails the raw gate; OOS doesn't matter)
    SOBREAJUSTADO  passed the K gate but the edge died/reversed out-of-sample
    INSUFICIENTE   passed the K gate but NO out-of-sample result is available
                   (we refuse to guess)

The pure decision function `combine_verdict(p, K, oos_confirmed)` has no
dependencies; `evaluate(...)` records the test via k_tracker (getting K) and then
applies the OOS layer; `validate_btc_oos(...)` actually runs validate.py to
produce both the raw p-value and the OOS confirmation for a BTC rule.

Stdlib only.
"""

import math

import k_tracker
from k_tracker import (KTracker, bonferroni_threshold, ALPHA,
                       VENTAJA_REAL, MARGINAL, RUIDO)

# Final-tier constants (the two new ones added at this stage).
SOBREAJUSTADO = "SOBREAJUSTADO"
INSUFICIENTE = "INSUFICIENTE"


# ---------------------------------------------------------------------------
# Raw p-value from a validate.py-style in-sample result
# ---------------------------------------------------------------------------

def _phi(z):
    """Standard normal CDF via erf (stdlib only)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def proportion_p_value(wins, n, base_rate):
    """One-sided p-value that the signal's win rate beats the base rate.

    H0: win rate == base_rate ; H1: win rate > base_rate. Normal approximation
    to the binomial (fine for the large n we get in-sample). Returns None when
    it can't be computed (no trades, or degenerate base rate) — so the caller
    can say INSUFICIENTE rather than invent a number.
    """
    if n is None or n <= 0:
        return None
    if base_rate <= 0.0 or base_rate >= 1.0:
        return None
    phat = wins / n
    se = math.sqrt(base_rate * (1.0 - base_rate) / n)
    if se == 0.0:
        return None
    z = (phat - base_rate) / se
    return 1.0 - _phi(z)  # upper tail: probability of doing this well by luck


# ---------------------------------------------------------------------------
# OOS result normalization
# ---------------------------------------------------------------------------

# Map the verdict strings emitted by validate.py / backtest_stock.py /
# halloween_test.py to a tri-state OOS confirmation: True / False / None.
_OOS_CONFIRMED = {"REAL EDGE", "REAL EDGE (HELD)", "CONFIRMED", "PROFITABLE"}
_OOS_FAILED = {"OVERFIT", "FRAGILE", "FADED", "NO EDGE", "NO EDGE / LOSES",
               "NOISE / WEAK", "CHANGED", "LOSES MONEY OUT-OF-SAMPLE"}


def map_oos_verdict(verdict_str):
    """Normalize an upstream verdict string to True / False / None.

    None means 'not determinable' (unknown label or no result) -> INSUFICIENTE.
    """
    if verdict_str is None:
        return None
    s = verdict_str.strip().upper()
    if s in _OOS_CONFIRMED:
        return True
    if s in _OOS_FAILED:
        return False
    return None


# ---------------------------------------------------------------------------
# The combination logic (pure)
# ---------------------------------------------------------------------------

def combine_verdict(p_value, K, oos_confirmed):
    """Combine the three gates into one final verdict + Spanish reason.

    oos_confirmed: True (held OOS), False (failed OOS), or None (no OOS result).
    Returns a dict: final, k_tier, K, threshold, oos_confirmed, explain.
    """
    k_tier = k_tracker.verdict(p_value, K)   # VENTAJA REAL / MARGINAL / RUIDO
    thr = bonferroni_threshold(K)

    def out(final, why):
        return {
            "final": final,
            "k_tier": k_tier,
            "K": K,
            "threshold": thr,
            "oos_confirmed": oos_confirmed,
            "explain": why,
        }

    # Gate 1: raw significance. If it fails, it's noise — OOS is irrelevant.
    if k_tier == RUIDO:
        return out(RUIDO,
                   f"p={p_value:.4f} ≥ {ALPHA}: no supera la prueba cruda de "
                   f"azar (falló la Puerta 1).")

    # It passed the K-adjusted gate (VENTAJA REAL) or at least raw (MARGINAL).
    # Gate 3: it MUST be confirmed out-of-sample.
    if oos_confirmed is None:
        return out(INSUFICIENTE,
                   f"Pasó la prueba estadística (p={p_value:.4f}, K={K}) pero no "
                   f"hay resultado out-of-sample (70/30) disponible; no se "
                   f"confirma ni se descarta (falta la Puerta 3).")
    if oos_confirmed is False:
        return out(SOBREAJUSTADO,
                   f"Significativo in-sample (p={p_value:.4f}) pero la ventaja "
                   f"se cae fuera de muestra (70/30): sobreajuste (falló la "
                   f"Puerta 3).")

    # oos_confirmed is True -> the final tier is whatever the K gate allowed.
    if k_tier == VENTAJA_REAL:
        return out(VENTAJA_REAL,
                   f"p={p_value:.4f} < umbral ajustado {thr:.4f} (K={K}) y "
                   f"confirmado out-of-sample: ventaja real en las 3 puertas.")
    return out(MARGINAL,
               f"p={p_value:.4f} < {ALPHA} pero ≥ umbral ajustado {thr:.4f} "
               f"(probaste K={K} variaciones); confirmado OOS pero solo marginal "
               f"(no pasó la Puerta 2).")


# ---------------------------------------------------------------------------
# Recording + combining (uses k_tracker for K)
# ---------------------------------------------------------------------------

def evaluate(tracker, user_id, hypothesis, formula, market, horizon, entry_cfg,
             p_value, oos_confirmed, sharpe=None):
    """Log the test (k_tracker assigns K), then apply the OOS layer.

    p_value None -> INSUFICIENTE is impossible to record (k_tracker requires a
    p-value); pass a real p-value. oos_confirmed may be None (-> INSUFICIENTE).
    """
    K = tracker.record_test(
        user_id=user_id, hypothesis=hypothesis, formula=formula,
        market=market, horizon=horizon, entry_cfg=entry_cfg,
        p_value=p_value, sharpe=sharpe,
    )
    result = combine_verdict(p_value, K, oos_confirmed)
    result["lifetime_k"] = tracker.lifetime_k(user_id)
    return result


# ---------------------------------------------------------------------------
# Real wiring to validate.py for a BTC rule
# ---------------------------------------------------------------------------

def validate_btc_oos(rule, predict="UP", data=None):
    """Run validate.py on a BTC rule and return (p_value, oos_confirmed).

    p_value: one-sided proportion test on the IN-SAMPLE result, or None if no
    in-sample trades. oos_confirmed: True if validate's verdict is REAL EDGE,
    False if it failed, None if the out-of-sample sample was too small to judge.
    """
    import validate

    path = data or validate.DATA_FILE
    rows, _ = validate.load_rows(path)
    if not rows:
        return None, None
    split = int(len(rows) * validate.IN_SAMPLE_FRACTION)
    code = validate.compile_rule(rule)
    is_stats = validate.evaluate(rows[:split], code, predict)
    oos_stats = validate.evaluate(rows[split:], code, predict)

    p_value = proportion_p_value(is_stats["wins"], is_stats["fired"],
                                 is_stats["base_rate"])

    # OOS confirmation, mirroring validate.py's own thresholds.
    if oos_stats["fired"] < validate.DEFAULT_MIN_SAMPLES:
        oos_confirmed = None  # too few OOS samples to judge -> INSUFICIENTE
    else:
        label, _ = validate.verdict(is_stats, oos_stats,
                                    validate.DEFAULT_EDGE_THRESHOLD,
                                    validate.DEFAULT_MIN_SAMPLES)
        oos_confirmed = (label == "REAL EDGE")
    return p_value, oos_confirmed


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _print_result(tag, r):
    print(f"  [{tag}] -> {r['final']}")
    print(f"        K={r['K']}  k_tier={r['k_tier']}  "
          f"oos_confirmed={r['oos_confirmed']}")
    print(f"        {r['explain']}")


def _demo():
    print("=" * 72)
    print("  FINAL VERDICT CHAIN  (raw p -> K-adjusted -> OOS-confirmed)")
    print("=" * 72)

    # --- Part A: real end-to-end on the BTC data via validate.py ----------
    print("\nPART A — real runs on btc_15m_data_v63.csv (one user, one idea):\n")
    try:
        tracker = KTracker(":memory:")
        cases = [
            ("momentum_3m > 0.5", "UP"),   # strong, persistent -> expect REAL
            ("rsi < 30", "UP"),            # wrong direction     -> expect RUIDO
            ("rsi < 30", "DN"),            # the validated edge  -> expect REAL
        ]
        for rule, predict in cases:
            p, oos = validate_btc_oos(rule, predict=predict)
            if p is None:
                print(f"  [{rule} -> {predict}] INSUFICIENTE: no in-sample "
                      f"trades to compute a p-value.")
                continue
            r = evaluate(
                tracker, user_id="leticia", hypothesis="rsi_momentum_btc",
                formula=f"{rule} :: predict {predict}", market="BTC",
                horizon="15m", entry_cfg={"stake": 10},
                p_value=p, oos_confirmed=oos,
            )
            _print_result(f"{rule} -> {predict}", r)
        print(f"\n  lifetime_k(leticia) = {tracker.lifetime_k('leticia')}")
        tracker.close()
    except FileNotFoundError:
        print("  (btc_15m_data_v63.csv not found — skipping Part A.)")

    # --- Part B: deterministic showcase of all five final tiers -----------
    print("\nPART B — crafted (p, K, oos) inputs to exhibit every final tier:\n")
    samples = [
        ("VENTAJA REAL",  0.0001, 5,  True),   # tiny p beats 0.05/5=0.01, OOS ok
        ("MARGINAL",      0.02,   5,  True),   # <0.05 but >0.01; OOS ok
        ("RUIDO",         0.20,   5,  True),   # fails raw 0.05 gate
        ("SOBREAJUSTADO", 0.0001, 5,  False),  # passes K gate, dies OOS
        ("INSUFICIENTE",  0.0001, 5,  None),   # passes K gate, no OOS result
    ]
    for expect, p, K, oos in samples:
        r = combine_verdict(p, K, oos)
        ok = "OK" if r["final"] == expect else f"!! expected {expect}"
        _print_result(f"p={p}, K={K}, oos={oos}  [{ok}]", r)
    print("=" * 72)


if __name__ == "__main__":
    _demo()
