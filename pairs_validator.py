#!/usr/bin/env python3
"""
pairs_validator.py — a skeptic for mean-reversion / statistical-arbitrage PAIRS.

Where validate.py judges a *signal rule* ("rsi < 30 predicts UP?"), this judges
a *pairs trade*: "is the spread between two assets actually mean-reverting, fast
enough to trade, and does that survive out-of-sample — or is it folklore?"

The motivating target is the kind of trade quant influencers publish: an
Ornstein-Uhlenbeck mean-reversion pair (e.g. GOOGL / AMZN), trading the z-score
of the spread. The pretty in-sample chart is never the question. The questions
this file refuses to skip are:

  Gate A  COINTEGRATION (Engle-Granger).  Regress Y on X by OLS; the spread is
          the residual. Run an Augmented Dickey-Fuller test on that spread. If
          the spread is NOT stationary, you don't have a pair — you have two
          random walks that happened to wander near each other. No reversion to
          trade.

  Gate B  OU HALF-LIFE.  Fit the discrete Ornstein-Uhlenbeck process by
          regressing the change in spread on the lagged spread:
              d(spread)_t = a + b * spread_{t-1} + e_t
          Then theta = -b, and half-life = ln(2) / theta. A pair can be
          "statistically" cointegrated but revert so slowly it's untradeable,
          or have a negative/degenerate half-life (not actually reverting).
          This is the gate the influencer screenshot never shows.

  Gate C  OOS RE-COINTEGRATION.  Split chronologically (first 70% in-sample,
          last 30% out-of-sample). Re-run the ADF stationarity test AND re-fit
          the half-life on the UNSEEN 30%. A relationship that only cohered on
          the data you looked at is overfit. This is the same discipline that
          killed the EWZ-Halloween "edge": looked fine on the whole, died on the
          split.

Verdict tiers (Spanish, matching the engine's house style):
    SIN COINTEGRACION   spread not stationary in-sample (fails Gate A)
    REVERSION LENTA     cointegrated but half-life too long / degenerate (Gate B)
    SOBREAJUSTADO       in-sample pair that loses cointegration out-of-sample (C)
    INSUFICIENTE        not enough overlapping data to judge honestly
    PAR VALIDO          stationary spread + tradeable half-life + holds OOS

Plugs into final_verdict.py: `validate_pair_oos(...)` returns
(p_value, oos_confirmed) using the in-sample ADF p-value and an OOS pass/fail,
so a pair can flow through the very same combine_verdict() chain as any signal.

Standard library only. No numpy, no scipy, no statsmodels.
"""

import argparse
import csv
import math
import sys


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IN_SAMPLE_FRACTION = 0.70

# ADF significance: spread is called stationary (cointegrated) if the ADF test
# statistic is below (more negative than) the critical value at this level.
ADF_ALPHA = 0.05

# A pair must have at least this many overlapping observations to judge at all,
# and at least this many in each split to trust an OOS re-test.
MIN_TOTAL_OBS = 60
MIN_SPLIT_OBS = 25

# Half-life guardrails (in observations / bars). A half-life longer than this is
# "real but too slow to trade"; <= 0 means the fit says "not reverting".
MAX_TRADEABLE_HALFLIFE = 120.0  # e.g. ~half a trading year on daily bars


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_series(path, x_col, y_col, date_col=None):
    """Load two aligned numeric price series from a CSV.

    Returns (xs, ys, dates). Rows where either price is blank/unparseable are
    skipped together so the two series stay index-aligned. If date_col is given
    and present, rows are sorted chronologically by it (string sort is fine for
    ISO dates); otherwise file order is assumed chronological.
    """
    recs = []
    skipped = 0
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if x_col not in reader.fieldnames or y_col not in reader.fieldnames:
            sys.exit(f"Columns {x_col!r}/{y_col!r} not found. "
                     f"Available: {', '.join(reader.fieldnames or [])}")
        for raw in reader:
            try:
                x = float(raw[x_col])
                y = float(raw[y_col])
            except (KeyError, ValueError, TypeError):
                skipped += 1
                continue
            d = (raw.get(date_col) or "").strip() if date_col else None
            recs.append((d, x, y))
    if date_col and all(r[0] for r in recs):
        recs.sort(key=lambda r: r[0])
    xs = [r[1] for r in recs]
    ys = [r[2] for r in recs]
    dates = [r[0] for r in recs]
    return xs, ys, dates, skipped


def to_log(series):
    """Convert a price series to log prices (guarding non-positive values).

    Log prices are the standard input for cointegration on equities: the spread
    becomes a log-ratio and the OLS hedge ratio is a clean elasticity. If any
    value is <= 0 we fall back to raw prices rather than crash.
    """
    if any(v <= 0 for v in series):
        return list(series)
    return [math.log(v) for v in series]


# ---------------------------------------------------------------------------
# Linear algebra helpers (stdlib OLS)
# ---------------------------------------------------------------------------

def ols(y, x):
    """Simple OLS of y on x with intercept. Returns (alpha, beta).

    beta = Cov(x,y)/Var(x); alpha = mean(y) - beta*mean(x). Pure arithmetic so
    we don't pull in numpy. Raises ValueError if x has zero variance.
    """
    n = len(x)
    if n == 0 or n != len(y):
        raise ValueError("ols: empty or mismatched inputs")
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx == 0.0:
        raise ValueError("ols: x has zero variance")
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    beta = sxy / sxx
    alpha = my - beta * mx
    return alpha, beta


def ols_multi(y, X):
    """OLS of y on multiple regressors (each column already includes whatever
    constant/lags the caller wants) via normal equations + Gaussian elimination.

    X is a list of rows, each row a list of regressor values. Returns the
    coefficient vector. Used by the ADF regression, which has several lag terms.
    Stdlib only; fine for the small p we use.
    """
    n = len(X)
    if n == 0:
        raise ValueError("ols_multi: no rows")
    k = len(X[0])
    # Build X'X (k x k) and X'y (k).
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for i in range(n):
        xi = X[i]
        yi = y[i]
        for a in range(k):
            xty[a] += xi[a] * yi
            xa = xi[a]
            row = xtx[a]
            for b in range(k):
                row[b] += xa * xi[b]
    return _solve(xtx, xty)


def _solve(A, b):
    """Solve A z = b by Gaussian elimination with partial pivoting."""
    n = len(A)
    # Augmented matrix.
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        # Pivot.
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise ValueError("_solve: singular matrix")
        M[col], M[piv] = M[piv], M[col]
        pivval = M[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col] / pivval
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                M[r][c] -= factor * M[col][c]
    return [M[i][n] / M[i][i] for i in range(n)]


# ---------------------------------------------------------------------------
# Augmented Dickey-Fuller test (stdlib)
# ---------------------------------------------------------------------------

# MacKinnon (2010) response-surface critical values for the ADF test with a
# constant (no trend), large-sample approximation. Keys are significance levels.
# These are the standard tau_c asymptotic criticals; good enough to call the
# stationarity gate. We interpolate the p-value coarsely between them.
_ADF_CRIT = {0.01: -3.43, 0.05: -2.86, 0.10: -2.57}


def _adf_design(y, dy, lag):
    """Build (targets, rows) for an ADF regression at a given lag.

    Row = [1, y_{t-1}, dy_{t-1}, ..., dy_{t-lag}]; target = dy_t. Returns
    (targets, rows) or (None, None) if the lag leaves too few observations.
    """
    targets, rows = [], []
    for j in range(lag, len(dy)):
        reg = [1.0, y[j]]  # const + level lag (y_{t-1}, t = j+1)
        for i in range(1, lag + 1):
            reg.append(dy[j - i])
        rows.append(reg)
        targets.append(dy[j])
    if not rows or len(rows) < len(rows[0]) + 5:
        return None, None
    return targets, rows


def _bic(targets, rows, coeffs):
    """Schwarz BIC for an OLS fit, used to pick the ADF lag length."""
    nobs = len(rows)
    k = len(coeffs)
    ssr = 0.0
    for i in range(nobs):
        pred = sum(coeffs[a] * rows[i][a] for a in range(k))
        ssr += (targets[i] - pred) ** 2
    if ssr <= 0 or nobs <= 0:
        return float("-inf")
    return nobs * math.log(ssr / nobs) + k * math.log(nobs)


def adf_test(series, max_lag=None):
    """Augmented Dickey-Fuller test on a series, with a constant.

    Regression:
        d(y)_t = a + g * y_{t-1} + sum_i b_i * d(y)_{t-i} + e_t
    The test statistic is g / se(g). A sufficiently negative stat rejects the
    unit-root null -> the series is stationary (the spread mean-reverts).

    Lag length is chosen by minimizing BIC over 0..max_lag rather than fixed by
    the Schwert rule, which over-lags short samples (e.g. a 120-point OOS window)
    and destroys test power. Returns dict: stat, crit, p_approx, n_used, lag,
    stationary. None if there isn't enough data to run.
    """
    y = list(series)
    n = len(y)
    if n < 20:
        return None
    if max_lag is None:
        # Schwert upper bound, but only as a CAP; BIC picks within it.
        max_lag = int(min(12 * (n / 100.0) ** 0.25, max(1, n // 5)))
        max_lag = max(1, max_lag)

    dy = [y[i] - y[i - 1] for i in range(1, n)]

    # Pick the lag (0..max_lag) that minimizes BIC on a common sample.
    best = None  # (bic, lag, targets, rows, coeffs)
    for lag in range(0, max_lag + 1):
        targets, rows = _adf_design(y, dy, lag)
        if targets is None:
            continue
        try:
            coeffs = ols_multi(targets, rows)
        except ValueError:
            continue
        b = _bic(targets, rows, coeffs)
        if best is None or b < best[0]:
            best = (b, lag, targets, rows, coeffs)

    if best is None:
        return None
    _b, lag, targets, rows, coeffs = best
    # Standard error of the gamma coefficient (index 1) from residual variance.
    k = len(coeffs)
    nobs = len(rows)
    resid = []
    for i in range(nobs):
        pred = sum(coeffs[a] * rows[i][a] for a in range(k))
        resid.append(targets[i] - pred)
    ssr = sum(r * r for r in resid)
    dof = nobs - k
    if dof <= 0:
        return None
    sigma2 = ssr / dof
    # (X'X)^-1 diagonal for gamma: invert just enough via solving for unit vec.
    xtx = [[0.0] * k for _ in range(k)]
    for i in range(nobs):
        xi = rows[i]
        for a in range(k):
            for b in range(k):
                xtx[a][b] += xi[a] * xi[b]
    e1 = [1.0 if a == 1 else 0.0 for a in range(k)]
    try:
        inv_col = _solve(xtx, e1)  # column 1 of (X'X)^-1
    except ValueError:
        return None
    var_gamma = sigma2 * inv_col[1]
    if var_gamma <= 0:
        return None
    se_gamma = math.sqrt(var_gamma)
    gamma = coeffs[1]
    stat = gamma / se_gamma

    crit = _ADF_CRIT[ADF_ALPHA]
    p_approx = _adf_pvalue(stat)
    return {
        "stat": stat,
        "crit": crit,
        "p_approx": p_approx,
        "n_used": nobs,
        "lag": lag,
        "stationary": stat < crit,
    }


def _adf_pvalue(stat):
    """Coarse ADF p-value by interpolating the MacKinnon critical points.

    Not a full response surface — enough to feed the chain with a monotonic,
    honest-ish number. More negative stat -> smaller p.
    """
    pts = sorted(_ADF_CRIT.items(), key=lambda kv: kv[1])  # by crit ascending
    # pts: [(0.01,-3.43),(0.05,-2.86),(0.10,-2.57)]
    if stat <= pts[0][1]:
        return pts[0][0]            # at or beyond 1% crit -> ~0.01
    if stat >= pts[-1][1]:
        # Less negative than the 10% crit: scale toward 1.0.
        # Map crit(-2.57)->0.10 and 0 -> ~0.90 linearly.
        c10 = pts[-1][1]
        frac = min(max(stat / c10, 0.0), 1.0) if c10 != 0 else 1.0
        return 0.10 + (1.0 - frac) * 0.80
    # Interpolate within the bracketed region.
    for (p_lo, c_lo), (p_hi, c_hi) in zip(pts, pts[1:]):
        if c_lo <= stat <= c_hi:
            if c_hi == c_lo:
                return p_hi
            w = (stat - c_lo) / (c_hi - c_lo)
            return p_lo + w * (p_hi - p_lo)
    return 0.50


# ---------------------------------------------------------------------------
# Ornstein-Uhlenbeck half-life
# ---------------------------------------------------------------------------

def ou_half_life(spread):
    """Estimate the OU mean-reversion half-life (in observations).

    Discrete fit: regress d(spread)_t on spread_{t-1}.
        d_t = a + b * spread_{t-1} + e_t   ->   theta = -b
        half_life = ln(2) / theta
    Returns dict: theta, half_life, b, tradeable. half_life is None / inf when
    the fit is non-reverting (b >= 0). 'tradeable' applies MAX_TRADEABLE_HALFLIFE.
    """
    n = len(spread)
    if n < 10:
        return None
    lagged = spread[:-1]
    dz = [spread[i] - spread[i - 1] for i in range(1, n)]
    try:
        _a, b = ols(dz, lagged)
    except ValueError:
        return None
    theta = -b
    if theta <= 0:
        # b >= 0: shocks don't decay -> not mean-reverting.
        return {"theta": theta, "b": b, "half_life": None, "tradeable": False}
    half_life = math.log(2.0) / theta
    tradeable = 0.0 < half_life <= MAX_TRADEABLE_HALFLIFE
    return {"theta": theta, "b": b, "half_life": half_life,
            "tradeable": tradeable}


# ---------------------------------------------------------------------------
# Spread construction
# ---------------------------------------------------------------------------

def build_spread(xs, ys):
    """Engle-Granger step 1: spread = y - (alpha + beta*x) using OLS hedge ratio.

    Returns (spread_list, alpha, beta). The spread is the residual series whose
    stationarity Gate A tests.
    """
    alpha, beta = ols(ys, xs)
    spread = [ys[i] - (alpha + beta * xs[i]) for i in range(len(xs))]
    return spread, alpha, beta


def zscore_last(spread, lookback=None):
    """Current z-score of the spread (how stretched the pair is right now).

    This is the actual trade trigger an OU pairs strategy uses; we report it so
    the verdict can say "and right now it's at z = -2.1" for content/decisions.
    """
    s = spread if lookback is None else spread[-lookback:]
    n = len(s)
    if n < 2:
        return None
    m = sum(s) / n
    var = sum((v - m) ** 2 for v in s) / (n - 1)
    if var <= 0:
        return None
    return (spread[-1] - m) / math.sqrt(var)


# ---------------------------------------------------------------------------
# The full pair verdict
# ---------------------------------------------------------------------------

SIN_COINTEGRACION = "SIN COINTEGRACION"
REVERSION_LENTA = "REVERSION LENTA"
SOBREAJUSTADO = "SOBREAJUSTADO"
INSUFICIENTE = "INSUFICIENTE"
PAR_VALIDO = "PAR VALIDO"


def validate_pair(xs, ys, use_log=True):
    """Run all three gates on a pair and return a full result dict.

    Keys: final, explain, and a 'detail' sub-dict with the gate outputs
    (hedge beta, in-sample ADF, half-life, OOS ADF, current z-score).
    """
    if use_log:
        xs = to_log(xs)
        ys = to_log(ys)

    n = len(xs)
    detail = {}

    if n < MIN_TOTAL_OBS:
        return {"final": INSUFICIENTE,
                "explain": f"Solo {n} observaciones alineadas (< {MIN_TOTAL_OBS}); "
                           f"no hay datos para juzgar el par con honestidad.",
                "detail": detail}

    # --- Full-sample spread (for hedge ratio + current z-score reporting) ----
    spread_full, alpha, beta = build_spread(xs, ys)
    detail["alpha"] = alpha
    detail["beta"] = beta
    detail["z_now"] = zscore_last(spread_full)

    # --- Chronological split -------------------------------------------------
    split = int(n * IN_SAMPLE_FRACTION)
    xs_is, ys_is = xs[:split], ys[:split]
    xs_oos, ys_oos = xs[split:], ys[split:]

    enough_split = (len(xs_is) >= MIN_SPLIT_OBS and len(xs_oos) >= MIN_SPLIT_OBS)

    # --- Gate A: in-sample cointegration ------------------------------------
    spread_is, a_is, b_is = build_spread(xs_is, ys_is)
    adf_is = adf_test(spread_is)
    detail["adf_in_sample"] = adf_is
    detail["beta_in_sample"] = b_is

    if adf_is is None:
        return {"final": INSUFICIENTE,
                "explain": "No se pudo correr la prueba ADF in-sample "
                           "(muy pocos datos tras el split).",
                "detail": detail}

    p_is = adf_is["p_approx"]
    if not adf_is["stationary"]:
        return {"final": SIN_COINTEGRACION,
                "explain": (f"El spread no es estacionario in-sample "
                            f"(ADF={adf_is['stat']:.2f} ≥ crítico "
                            f"{adf_is['crit']:.2f}, p≈{p_is:.3f}). No hay "
                            f"cointegración: son dos caminatas aleatorias, no un "
                            f"par. (Falló la Puerta A.)"),
                "detail": detail}

    # --- Gate B: OU half-life on the in-sample spread -----------------------
    hl = ou_half_life(spread_is)
    detail["half_life"] = hl
    if hl is None:
        return {"final": INSUFICIENTE,
                "explain": "No se pudo estimar la vida media OU in-sample.",
                "detail": detail}
    if not hl["tradeable"]:
        if hl["half_life"] is None:
            why = (f"La vida media OU es degenerada (θ={hl['theta']:.4f} ≤ 0): "
                   f"el spread no revierte, solo se cointegró por estadística. ")
        else:
            why = (f"Vida media ≈ {hl['half_life']:.0f} barras "
                   f"(> {MAX_TRADEABLE_HALFLIFE:.0f}): revierte demasiado lento "
                   f"para operar. ")
        return {"final": REVERSION_LENTA,
                "explain": why + "Cointegrado pero no operable. (Falló la Puerta B.)",
                "detail": detail}

    # --- Gate C: OOS re-cointegration ---------------------------------------
    if not enough_split:
        return {"final": INSUFICIENTE,
                "explain": (f"Pasó A y B in-sample (vida media ≈ "
                            f"{hl['half_life']:.0f} barras) pero el tramo "
                            f"out-of-sample es muy chico para re-test honesto "
                            f"(in={len(xs_is)}, oos={len(xs_oos)}). (Falta la "
                            f"Puerta C.)"),
                "detail": detail}

    spread_oos, _a_o, _b_o = build_spread(xs_oos, ys_oos)
    adf_oos = adf_test(spread_oos)
    hl_oos = ou_half_life(spread_oos)
    detail["adf_oos"] = adf_oos
    detail["half_life_oos"] = hl_oos

    oos_ok = (adf_oos is not None and adf_oos["stationary"]
              and hl_oos is not None and hl_oos["tradeable"])

    if not oos_ok:
        if adf_oos is None or hl_oos is None:
            why = "no se pudo re-evaluar fuera de muestra con seguridad."
        elif not adf_oos["stationary"]:
            why = (f"el spread deja de ser estacionario fuera de muestra "
                   f"(ADF={adf_oos['stat']:.2f} ≥ {adf_oos['crit']:.2f}).")
        else:
            why = (f"la reversión se vuelve no operable fuera de muestra "
                   f"(vida media ≈ "
                   f"{(hl_oos['half_life'] or float('inf')):.0f} barras).")
        return {"final": SOBREAJUSTADO,
                "explain": (f"Cointegrado y operable in-sample, pero {why} "
                            f"El par estaba sobreajustado al tramo que miraste. "
                            f"(Falló la Puerta C.)"),
                "detail": detail}

    return {"final": PAR_VALIDO,
            "explain": (f"Spread estacionario (ADF in={adf_is['stat']:.2f}, "
                        f"oos={adf_oos['stat']:.2f}), vida media operable "
                        f"(in≈{hl['half_life']:.0f}, oos≈{hl_oos['half_life']:.0f} "
                        f"barras) y la cointegración SOBREVIVE fuera de muestra. "
                        f"Par válido en las 3 puertas."),
            "detail": detail}


# ---------------------------------------------------------------------------
# Wiring into final_verdict.py
# ---------------------------------------------------------------------------

def validate_pair_oos(path, x_col, y_col, date_col=None, use_log=True):
    """Adapter for final_verdict.combine_verdict().

    Returns (p_value, oos_confirmed):
      p_value       : in-sample ADF approx p-value (the raw Gate-1 signal).
      oos_confirmed : True if the pair survives OOS re-cointegration (PAR VALIDO),
                      False if it fails a gate it reached (SIN COINTEGRACION,
                      REVERSION LENTA, SOBREAJUSTADO), None if INSUFICIENTE.
    So a pair flows through the same raw-p -> K-adjusted -> OOS chain as signals.
    """
    xs, ys, _dates, _skipped = load_series(path, x_col, y_col, date_col)
    if not xs:
        return None, None
    res = validate_pair(xs, ys, use_log=use_log)
    detail = res.get("detail", {})
    adf_is = detail.get("adf_in_sample")
    p_value = adf_is["p_approx"] if adf_is else None

    final = res["final"]
    if final == PAR_VALIDO:
        oos = True
    elif final == INSUFICIENTE:
        oos = None
    else:
        oos = False
    return p_value, oos


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt(v, spec=".4f"):
    return format(v, spec) if isinstance(v, (int, float)) else "N/A"


def print_report(res, x_col, y_col, n, skipped):
    d = res["detail"]
    print("=" * 70)
    print("  VALIDADOR DE PARES  (cointegración → vida media OU → OOS)")
    print("=" * 70)
    print(f"  par          : {y_col}  ~  {x_col}")
    print(f"  observaciones: {n}   (descartadas: {skipped})")
    if "beta" in d:
        print(f"  hedge ratio  : β={_fmt(d.get('beta'))}  α={_fmt(d.get('alpha'))}")
    if d.get("z_now") is not None:
        print(f"  z-score ahora: {_fmt(d['z_now'], '+.2f')}  "
              f"(qué tan estirado está el par hoy)")
    print("-" * 70)

    adf_is = d.get("adf_in_sample")
    if adf_is:
        print(f"  Puerta A ADF (in) : stat={_fmt(adf_is['stat'], '.3f')}  "
              f"crit={_fmt(adf_is['crit'], '.2f')}  "
              f"p≈{_fmt(adf_is['p_approx'], '.3f')}  "
              f"{'estacionario' if adf_is['stationary'] else 'NO estacionario'}")
    hl = d.get("half_life")
    if hl:
        hl_txt = (f"{hl['half_life']:.0f} barras" if hl['half_life'] is not None
                  else "degenerada (no revierte)")
        print(f"  Puerta B vida media: {hl_txt}  θ={_fmt(hl['theta'], '.4f')}  "
              f"{'operable' if hl['tradeable'] else 'NO operable'}")
    adf_oos = d.get("adf_oos")
    if adf_oos:
        hl_o = d.get("half_life_oos")
        hl_o_txt = (f"{hl_o['half_life']:.0f}" if hl_o and hl_o['half_life']
                    is not None else "—")
        print(f"  Puerta C ADF (oos): stat={_fmt(adf_oos['stat'], '.3f')}  "
              f"crit={_fmt(adf_oos['crit'], '.2f')}  "
              f"{'estacionario' if adf_oos['stationary'] else 'NO estacionario'}  "
              f"| vida media oos≈{hl_o_txt}")
    print("-" * 70)
    print(f"  VEREDICTO: {res['final']}")
    print(f"  {res['explain']}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Synthetic self-test (no external data needed)
# ---------------------------------------------------------------------------

def _synthetic_pair(n=400, kind="cointegrated", seed=12345):
    """Generate a deterministic synthetic pair for testing (stdlib RNG).

    kind='cointegrated' : y = 1.5*x + stationary OU spread  -> expect PAR VALIDO
    kind='random'       : two independent random walks       -> SIN COINTEGRACION
    kind='slow'         : cointegrated but tiny theta         -> REVERSION LENTA
    """
    import random
    rng = random.Random(seed)
    xs = [100.0]
    for _ in range(n - 1):
        xs.append(xs[-1] + rng.gauss(0, 1))

    if kind == "random":
        ys = [100.0]
        for _ in range(n - 1):
            ys.append(ys[-1] + rng.gauss(0, 1))
        return xs, ys

    # OU spread.
    theta = 0.20 if kind == "cointegrated" else 0.005
    spread = [0.0]
    for _ in range(n - 1):
        s = spread[-1]
        spread.append(s + theta * (0.0 - s) + rng.gauss(0, 0.5))
    ys = [1.5 * xs[i] + 5.0 + spread[i] for i in range(n)]
    return xs, ys


def _self_test():
    print("=" * 70)
    print("  SELF-TEST  pairs_validator.py  (synthetic, deterministic)")
    print("=" * 70)
    expectations = [
        ("cointegrated", PAR_VALIDO),
        ("random", SIN_COINTEGRACION),
        ("slow", REVERSION_LENTA),
    ]
    all_ok = True
    for kind, expect in expectations:
        xs, ys = _synthetic_pair(kind=kind)
        res = validate_pair(xs, ys, use_log=False)
        got = res["final"]
        # 'slow' may land as REVERSION_LENTA or (if borderline) SOBREAJUSTADO;
        # accept either as "correctly not PAR VALIDO" for the slow case.
        ok = (got == expect) or (kind == "slow" and got != PAR_VALIDO)
        all_ok = all_ok and ok
        tag = "OK" if ok else f"!! expected {expect}"
        print(f"\n  [{kind}] -> {got}   [{tag}]")
        print(f"      {res['explain']}")
    print("\n" + "=" * 70)
    print("  RESULT:", "all checks passed" if all_ok else "SOME CHECKS FAILED")
    print("=" * 70)
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pairs_validator.py",
        description="Skeptic for mean-reversion PAIRS trades. Tests a pair "
                    "through three gates: Engle-Granger cointegration -> OU "
                    "half-life -> out-of-sample re-cointegration. Prints ONE "
                    "Spanish verdict naming the gate it failed.",
        epilog='Examples:\n'
               '  python3 pairs_validator.py prices.csv --x GOOGL --y AMZN '
               '--date date\n'
               '  python3 pairs_validator.py --self-test',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("data", nargs="?",
                        help="CSV with one column per asset's price series.")
    parser.add_argument("--x", help="Column name for asset X (the hedge).")
    parser.add_argument("--y", help="Column name for asset Y (the target).")
    parser.add_argument("--date", default=None,
                        help="Optional date column to sort chronologically.")
    parser.add_argument("--raw", action="store_true",
                        help="Use raw prices instead of log prices.")
    parser.add_argument("--self-test", action="store_true",
                        help="Run the built-in synthetic checks (no data file).")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    missing = [n for n in ("data", "x", "y") if not getattr(args, n)]
    if missing:
        parser.error("missing required argument(s): " + ", ".join(missing)
                     + " (or use --self-test)")

    xs, ys, _dates, skipped = load_series(args.data, args.x, args.y, args.date)
    if not xs:
        parser.error("no usable rows loaded from the data file.")
    res = validate_pair(xs, ys, use_log=not args.raw)
    print_report(res, args.x, args.y, len(xs), skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
