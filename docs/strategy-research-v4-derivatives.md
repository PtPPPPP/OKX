# Strategy Research V4 — Derivatives Information Edge (Design Only)

Status: DESIGN. No V4 signal, backtest, or candidate has been implemented in this
document's revision. This file only freezes the next research direction and its gates.

## 1. Motivation

- Pure VWAP research is frozen: `WALK_FORWARD_REGIME_FRAGILE`, final holdout -16.1%,
  Sharpe -0.79, and `PURE_VWAP_RESEARCH_STOP_RECOMMENDED=true`
  (`artifacts/backtests/VWAP_WALK_FORWARD_V1_20260812T144106Z/report.md`).
- Strategy Research V3 (5 candidates from OHLCV + HTF + volume) ended with
  `NO_STRATEGY_CANDIDATE_FOUND_V3`; none passed the `forward_edge` gate.
- The next research line must draw on information the previous rounds did not
  consume: derivatives-state variables, not more OHLCV parameter search.

## 2. Data inventory (verified against code and artifacts)

Verified sources exist in `backtest/derivatives_collector.py`
(`OKXDerivativesPublicClient`) and four collector runs under
`artifacts/prospective-oos/DERIVATIVES_PROSPECTIVE_COLLECTOR_V1_*`
(latest 2026-08-13), all `READY` in `data_quality_report.json`:

| Variable | Status | Source |
|---|---|---|
| Funding rate (history + current) | AVAILABLE | `/api/v5/public/funding-rate-history`, `/api/v5/public/funding-rate` |
| Open interest (history + current) | AVAILABLE | `/api/v5/rubik/stat/contracts/open-interest-history`, `/api/v5/public/open-interest` |
| Mark-price candles | AVAILABLE | `/api/v5/market/history-mark-price-candles` |
| Index-price candles | AVAILABLE | `/api/v5/market/history-index-candles` |
| Basis (mark vs spot) | AVAILABLE (derived) | `derived/basis_mark_spot` partition, computed by the collector |
| Perp + spot candles (divergence) | AVAILABLE | swap + spot `history-candles` |
| Perp premium | PARTIAL | derivable from mark/index/funding; explicit `/api/v5/public/premium-history` not wired |
| Liquidations / forced flow | MISSING | no endpoint support today (`/api/v5/public/liquidation-orders` unwired) |
| Volatility regime | PARTIAL | derivable from existing candles; no new feed required |

Constraints discovered during inventory:

- The prospective collector has not run since 2026-08-13; the accumulating
  prospective set is ~stale and must be restarted (authorized, networked) before
  any V4 prospective validation.
- OKX Rubik OI history granularity is 5m with limited history depth; the exact
  reachable backfill depth for OI and funding must be measured in V4 Phase 0
  before candidate design, so that no candidate depends on data that cannot be
  obtained.

## 3. Phase 0 — data pipeline validation (required before any signal)

1. Re-run the derivatives prospective collector for a bounded, authorized
   window and confirm `data_asset_health` stays `READY` for all sources.
2. Measure and record max reachable history depth per source (funding, OI,
   mark/index candles, basis) into a data manifest artifact.
3. Decide explicitly whether liquidation data is worth wiring
   (`MISSING` today) or excluded from V4 scope. Default: excluded until a
   candidate family demonstrably needs it.
4. Reuse the existing prospective-OOS firewall: future confirmed data may only
   validate a frozen candidate; it never flows back into design.

## 4. Candidate families to consider (frozen only after Phase 0)

Design directions, in priority order, all long-only spot-execution-compatible
or explicitly rejected by the safety contract first:

1. **Funding-rate pressure**: sustained one-sided funding as a contrarian or
   continuation regime variable on 1H BTC-USDT.
2. **Open-interest expansion/contraction**: OI change conditioned price moves
   (breakout quality, exhaustion) — extends V3's `htf_*` rejects with a state
   variable they lacked.
3. **Basis / premium regime**: mark-spot basis percentile regimes gating
   existing entry families; premium from mark vs index where derivable.
4. **Spot-perp divergence**: divergence episodes between spot and perp candles
   as short-horizon information.

Each family must define, before any backtest: episode definition, incremental
value test versus an OHLCV-only control, and the exact reject gate.

## 5. Governance (unchanged from V1–V3)

- Parameter freeze before evaluation; no post-hoc window/threshold tuning.
- Walk-forward with strict next-window OOS; untouched final holdout never used
  for selection or tuning.
- Cost stress, profit concentration, random-timing benchmark, temporal
  stability, and prospective-OOS validation for any surviving candidate.
- A candidate passes only with: forward evidence, untouched holdout positive
  after costs, cost robustness, acceptable profit concentration, and a
  meaningful random benchmark. Thresholds reuse the existing research protocol;
  no new looser gates.
- `NO_STRATEGY_CANDIDATE` remains an acceptable final answer. Infrastructure
  investment is not a reason to force a positive conclusion.

## 6. Scope guards

- V4 research is offline, read-only, and public-API only; it never touches the
  demo order path, the submission fence, or the production database.
- Continuous Demo remains frozen until Phase 4A passes, Phase 4B
  (fault/recovery) passes, and a strategy candidate clears the research
  protocol. Live remains fail-closed permanently by the safety contract.
