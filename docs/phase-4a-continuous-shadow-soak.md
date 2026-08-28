# Phase 4A Continuous Shadow 24h Soak

## Purpose

Phase 4A verifies that the existing public-market Continuous VWAP Shadow can remain healthy during
a real 24-hour wall-clock run. It observes network lifecycle, confirmed-candle continuity, atomic
SQLite persistence, heartbeat/lock renewal, resource growth and graceful shutdown.

The soak is read-only with respect to OKX. A process-local guard replaces every known Demo order,
cancellation, controlled-write, authorization-consumption and Demo Broker write method with an
immediate failure. The final report also requires zero orders, fills and submitted Shadow Proposals
in the dedicated soak database.

## Frozen runtime path

```text
python -m app.cli run-vwap-continuous-shadow
→ app.continuous_shadow_cli
→ OKXPublicHistoricalDataProvider + OKXPublicWebSocketProvider
→ ContinuousVWAPShadowRunner.run_events
→ confirmed BTC-USDT 1H Candle
→ VWAPShadowStrategy(window=24, buy_deviation_bps=100)
→ Signal / non-executable Shadow Proposal
→ ContinuousShadowRepository.commit_vwap_shadow_candle
→ processed candle + runtime state + signal/proposal + heartbeat (one transaction)
→ ContinuousRunLock / finish / release
```

Phase 4A tooling reuses this runner API. It does not implement a second strategy or trading engine.

## Fixed configuration

```text
config=configs/btc_vwap_shadow.yaml
instrument=BTC-USDT
bar_interval=1h
strategy=vwap_shadow
vwap_window=24
buy_deviation_bps=100
market_data=OKX public REST + public WebSocket
private_api=false
```

## Network configuration

The canonical network layer supports:

```text
OKX_NETWORK_MODE=env     # default; honor HTTP(S)_PROXY environment variables
OKX_NETWORK_MODE=direct  # ignore environment proxies
OKX_NETWORK_MODE=proxy   # require OKX_PROXY_URL
```

Example for a user-supplied local proxy:

```powershell
$env:OKX_NETWORK_MODE = "proxy"
$env:OKX_PROXY_URL = "http://127.0.0.1:<PORT>"
```

Do not disable TLS verification. The proxy URL itself is never written to soak artifacts; artifacts
record only the network mode and whether an explicit proxy was configured.

## Tooling

```powershell
uv run python scripts/phase_4a_soak.py --help
```

The tool owns only operational orchestration:

- a new database under `data/soak/phase_4a/`;
- an active runtime generation for that new database;
- lock renewal and heartbeat every 10 seconds;
- append-and-fsync evidence records;
- detached process lifecycle on Windows;
- status, graceful stop and deterministic finalization.

It refuses to reuse an existing database.

### Process liveness probing

`status`, `stop` and `finalize` share one liveness probe that is strictly
read-only: it never signals, interrupts or terminates the probed process.

- Windows: `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` plus
  `GetExitCodeProcess` (`STILL_ACTIVE` = 259 means running). `os.kill(pid, 0)`
  is never used on Windows because signal 0 aliases `CTRL_C_EVENT` and can
  raise Ctrl+C inside the target console process.
- POSIX: `os.kill(pid, 0)`, which the kernel answers without delivery.
- A failed `OpenProcess` with `ERROR_INVALID_PARAMETER` means the PID does not
  exist; every other failure (including `ERROR_ACCESS_DENIED` from a live
  process refusing query access) fails closed and keeps reporting the process
  as alive, because wrongly declaring a running soak dead is the unsafe error.
- PID values `<= 0` are refused without any OS call.

Because the probe reads real OS state rather than artifact status, `status`
still detects a dead process behind an artifact that claims to be running.

## Short shakedown

Start a bounded detached shakedown:

```powershell
uv run python scripts/phase_4a_soak.py start `
  --duration-seconds 180 `
  --sample-interval-seconds 5 `
  --network-mode proxy `
  --proxy-url http://127.0.0.1:<PORT>
```

The command returns JSON containing `run_id`, `process_id`, `start_utc`, `target_end_utc`, database
and artifact paths only after REST bootstrap and a real public WebSocket connection succeed.

Read status using the returned artifact path:

```powershell
uv run python scripts/phase_4a_soak.py status `
  --artifact-dir artifacts/soak/phase_4a/<SOAK_ID>
```

Request graceful shutdown:

```powershell
uv run python scripts/phase_4a_soak.py stop `
  --artifact-dir artifacts/soak/phase_4a/<SOAK_ID> `
  --wait-seconds 60
```

Rebuild the final report after the process exits:

```powershell
uv run python scripts/phase_4a_soak.py finalize `
  --artifact-dir artifacts/soak/phase_4a/<SOAK_ID>
```

Shakedown must prove:

```text
public_ws_connects >= 1
exchange_write_attempts = 0
database_integrity = true
uncaught_exceptions = 0
heartbeat_not_stuck = true
runtime_state_consistent = true
graceful_shutdown = true
pending_tasks_after_shutdown = 0
artifact_valid = true
```

With a 1-hour candle interval, a short shakedown may legitimately finish without receiving a closed
candle. Strategy and candle-continuity evidence is accumulated by the real 24-hour run.

## Start the real 24-hour soak

Only after shakedown passes:

```powershell
uv run python scripts/phase_4a_soak.py start `
  --duration-seconds 86400 `
  --sample-interval-seconds 300 `
  --network-mode proxy `
  --proxy-url http://127.0.0.1:<PORT>
```

The detached process continues if the Codex task or terminal closes. It stops automatically at the
target end time, or earlier when the explicit stop command creates `stop.requested`.

## Artifacts

Each run writes to:

```text
artifacts/soak/phase_4a/<SOAK_ID>/
  metadata.json
  samples.jsonl
  runtime.log
  runtime_summary.json
  final_report.json
```

`samples.jsonl` is append-only and each record is flushed and fsynced. Metadata and final reports are
atomically replaced. The artifact schema rejects credential-bearing field names and exact credential
values present in the process environment.

Artifacts and soak databases are gitignored and must not be committed.

## 24-hour acceptance

```text
duration_target_met = true
exchange_write_attempts = 0
database_integrity = true
duplicate_persisted_candles = 0
out_of_order_persisted_candles = 0
unexplained_missing_persisted_candles = 0
uncaught_exceptions = 0
heartbeat_not_stuck = true
runtime_state_consistent = true
graceful_shutdown = true
pending_tasks_after_shutdown = 0
```

Natural reconnects are recorded. If none occurs, the result is `RECONNECT_NOT_EXERCISED`; Phase 4A
does not manufacture a failure merely to exercise recovery.

## What this does not prove

- strategy profitability, PnL, win rate or Sharpe;
- Demo continuous execution;
- live trading readiness;
- forced network outage recovery;
- process-kill recovery;
- injected database locking;
- operating-system crash durability;
- power-loss durability.

Those failure-injection questions belong to a later, separately authorized phase.
