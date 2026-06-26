# Auditoría de datos + Verdict del edge "semis baratos por forward P/E"

**Fecha:** 2026-06-26 · **Branch:** `claude/mu-analysis-semi-edge-6fvz8u`

Documento gemelo del análisis de MU en `quantdesk2` (`MU_ANALYSIS_REPORT.md`). Aquí cubrimos lo que concierne a este repo: la **auditoría de datos (Tarea 0)** y el **verdict del edge sistemático (Tarea 2)**.

---

## Tarea 0 — Auditoría de datos de frankenstein-bot

| Fuente / archivo | Tipo de dato | Cobertura (tickers · rango · frecuencia) | ¿Apto para backtest forward-P/E de equities? |
|---|---|---|---|
| `btc_15m_data_v61..v63.csv` (+ backfill) | OHLCV + indicadores | **Sólo BTC** · ~nov 2025→ · 15-min | ❌ No (cripto) |
| `frank_mega_training.csv` | Orderbook Binance + Polymarket + Black-Scholes | BTC, ETH · abr 2026 · tick/seg | ❌ No (cripto) |
| `frank_v15_training.csv` | BTC + Polymarket oracle lag | BTC · abr 2026 · tick/seg | ❌ No (cripto) |
| `frank_math_training.csv`, `frank_v15_session.csv` | Features ML / resúmenes | BTC + PM · abr 2026 | ❌ No (cripto) |
| `book_snapshots.csv` | Orderbook Polymarket | BTC, ETH, SOL · feb 2025 · 15-min | ❌ No (cripto) |
| `order_log_v70.csv`, `trades_v62..v70.csv` | Logs de trades simulados | BTC, SOL · feb 2026 · por-trade | ❌ No (cripto) |

**Resumen de cobertura:**
- **Precios históricos de equities:** ❌ **NINGUNO.** Cero CSV de acciones. `backtest_stock.py` hace fetch on-demand a Yahoo Finance (host **bloqueado** por la política de red del entorno → 403).
- **Fundamentales históricos (P/E, forward P/E, EPS, revenue/trimestre, estimates):** ❌ **NINGUNO.** No hay conector a Finnhub/AlphaVantage/FMP. `earnings_reaction.py` aproxima earnings por anomalías de volumen, no usa datos oficiales.
- **APIs configuradas:** Binance (REST/WS/FAPI), Polymarket (GAMMA/CLOB), Chainlink — **todas cripto**. **No** hay Finnhub/FRED/Banxico.

**Motor de validación (reutilizable, agnóstico al activo):** `k_tracker.py` (Bonferroni K + fingerprints + budget), `final_verdict.py` (ladder: VENTAJA REAL/MARGINAL/RUIDO/SOBREAJUSTADO/INSUFICIENTE), `validate.py` (in-sample vs OOS 70/30), `backtest_stock.py` (RSI sobre cierres, necesita Yahoo o `--csv`), `momentum_xs.py` (momentum cross-sectional, necesita N CSV por acción), `pairs_validator.py` (cointegración + OU). `ladder_scanner.py` es sólo Polymarket (no reutilizable).

**Conclusión Tarea 0:** existe el **motor de disciplina estadística**, pero **no existe la capa de datos** (ni precios de equities, ni fundamentales, ni estimates point-in-time). No se puede alimentar un backtest de forward-P/E de semis con datos reales en este repo/entorno.

---

## Tarea 2 — Verdict del edge sistemático

**Hipótesis:** "Comprar semis bajo la mediana de forward P/E del grupo y rebalancear (mensual/trimestral) genera retorno ajustado por riesgo positivo OOS."

### Verdict preliminar: `DATOS INSUFICIENTES`

Esto es un resultado **válido y correcto**, no un fallo. El brief instruye **no correr el pipeline hasta confirmación del usuario basada en la auditoría**; este verdict está **forzado por la auditoría** y se sostiene contra los tres controles anti-autoengaño:

1. **Tamaño de muestra OOS = 0.** No hay precios ni fundamentales históricos de equities. **MinTRL** mata la prueba como DATOS INSUFICIENTES antes de calcular cualquier Sharpe.
2. **Look-ahead inevitable.** El forward P/E usa *estimates*. Sólo es accesible el estimate **actual** (jun-2026), no el **point-in-time** de cada fecha de rebalanceo. Sin estimates históricos point-in-time, todo backtest tiene look-ahead y **no es válido**.
3. **Survivorship bias.** Un universo con los 12 tickers sobrevivientes de hoy (SNDK ni cotizaba antes de feb-2025; faltan deslistados/adquiridos) sesga al alza.

**Gates DSR / MinTRL / Bonferroni (`k_tracker`):** no producen un número significativo sin serie OOS. Registrar el fingerprint contaría contra el budget de pruebas, pero el resultado por construcción es DATOS INSUFICIENTES.

### Para correrlo de verdad (si se confirma proceder) se necesita:
1. Precios diarios históricos de los 12 tickers — **universo point-in-time**, incluyendo nombres deslistados/adquiridos (anti-survivorship).
2. **Consensus forward-EPS point-in-time** por fecha de rebalanceo (la pieza más difícil/cara: IBES/FactSet/Refinitiv). Sin esto no se pasa el control de look-ahead.
3. Pipeline: cargar en `validate.py` / `momentum_xs.py` → `final_verdict.py` → `k_tracker.py` (fingerprint + Bonferroni + DSR/MinTRL). **No** escribir un motor nuevo.

---

### Separación clave (el valor del ejercicio)
- **Empresa (MU):** bien posicionada hoy (HBM sold-out, ~$100B contratado).
- **Acción (MU ~$1,162 / ~8x fwd):** ~8x es firma de **pico de ciclo**, posible value trap si los márgenes-pico revierten.
- **Edge sistemático:** **DATOS INSUFICIENTES** — no demostrable OOS con los datos disponibles.

Las tres respuestas son distintas, y eso es exactamente lo correcto.
