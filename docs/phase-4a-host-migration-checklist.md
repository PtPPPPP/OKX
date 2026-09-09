# Phase 4A 24h Host Migration — Fresh-Host Checklist

Status: PREPARED (2026-09-09). The dorm host has a fixed nightly power outage and
cannot provide reliable 24h wall-clock execution, so the Phase 4A Continuous
Shadow 24h acceptance moves to a dedicated host with >=30h continuous power and
network. This document is the operating checklist for that host.

## 1. Scope and prohibitions (unchanged by this migration)

- This migration changes no trading logic, WebSocket policy, strategy, DB
  schema, demo, or live path. The code baseline in section 2 is frozen.
- Phase 4A stays public-only: no API key, secret, passphrase, or private
  credential may be copied to the new host. The repository tracks only
  `.env.example` (a template) and contains no secrets.
- Not allowed for 24h acceptance: pause/resume stitching, multiple runs stitched
  into 24h, automatic restart continuation, or editing artifact durations.
- Do not enter Phase 4B, Continuous Demo, or Live from this migration.

## 2. Authoritative baseline

- CODE_BASELINE: `789312c7af6f6e6ec3d3bbe919a250bba4555f82` (= origin/main at
  freeze time, working tree clean). `789312c` passed CI on windows-latest and
  ubuntu-latest; its parent `2aedd2e` (the websocket reconnect-budget fix,
  regression-tested fail-before/pass-after) passed the same matrix.
- This checklist is a docs-only addition on top of `789312c`; production code
  is identical. Verify on the fresh host after checkout:
  `git diff --stat 789312c HEAD` shows only files under `docs/`.

## 3. Dorm run disposition (host that cannot finish)

- Run `20260909T052205Z-5b54f260` (started 2026-09-09T05:22:06Z on `789312c`,
  public WS connected, zero exchange writes) is deliberately kept running
  until the fixed nightly power cut ends it.
- Artifacts preserved under `artifacts/soak/phase_4a/20260909T052205Z-5b54f260/`
  and `data/soak/phase_4a/20260909T052205Z-5b54f260.db`. Do not delete or edit.
- Mandatory classification once the power cut ends it:
  - `classification = SYSTEM_INTERRUPTION`
  - `project_runtime_failure = false`
  - `duration_target_met = false`
  - MUST NOT be marked PASS.
- After power returns, rebuild the final report from evidence (no artifact
  mutation): `uv run python scripts/phase_4a_soak.py finalize --artifact-dir artifacts/soak/phase_4a/20260909T052205Z-5b54f260`
- The soak DB runs WAL + synchronous FULL, so a hard power cut cannot corrupt
  committed transactions; `finalize` re-derives the report from evidence.

## 4. Fresh-host checklist

Host prerequisites (before anything else):

- [ ] Continuous power guaranteed >=30h (UPS or non-dorm supply)
- [ ] Continuous network >=30h (no captive portal, no scheduled disconnect)

### 4.1 Git clone / checkout exact commit

```bash
git clone https://github.com/PtPPPPP/OKX.git
cd OKX
git rev-parse HEAD        # record this SHA in the migration report
git diff --stat 789312c HEAD   # must show only docs/ differences
```

### 4.2 Python / uv version

- Python 3.12 (matches CI `python-version: "3.12"`; pyproject requires >=3.11).
- Install uv (any recent release; reference versions: local 0.11.x, CI uses
  astral-sh/setup-uv@v6).
- Locked dependencies: `uv sync --extra dev` (the exact CI command; uses
  `uv.lock`, no floating resolution).

### 4.3 Config presence

- [ ] `configs/btc_vwap_shadow.yaml` exists (tracked in git).
- [ ] The `config_hash` recorded in the run's `startup_evidence.json` equals
  `6688baa20b6eeb79ec45a899bef7c487c16da0ff4541355afaa763247ea365a6`
  (same file as the authorized dorm attempt).

### 4.4 Artifact / data gitignore

- After `uv sync` and after every validation run, `git status` stays clean:
  `artifacts/`, `data/`, `.venv/`, caches are all gitignored.

### 4.5 System clock

- [ ] NTP enabled; system clock within a few seconds of UTC.
- Cross-check: `startup_evidence.network.system_clock_utc` must match actual
  UTC at start time.

### 4.6 DNS / network

- [ ] OKX hostnames resolve (e.g. `nslookup www.okx.com` / `dig www.okx.com`).

### 4.7 OKX public REST

- [ ] Covered by the shakedown's `startup_evidence.rest_bootstrap = true`
  (section 6). No separate tooling required.

### 4.8 OKX public WS

- [ ] Covered by the shakedown's `startup_evidence.public_ws_connected = true`.

### 4.9 Proxy requirement

- Try the simplest path first: `--network-mode direct`. Do NOT deploy FlClash
  just to mirror the dorm environment.
- If direct fails (DNS policy block / connection reset), deploy the simplest
  working proxy on that host (any HTTP proxy) and run with
  `--network-mode proxy --proxy-url <URL>`.
- Any network configuration difference from the dorm run (direct vs proxy,
  proxy software, URL) MUST be recorded explicitly in the migration report.

### 4.10 TLS

- Standard OS install suffices (the venv ships certifi via `uv sync`). If the
  host has corporate MITM certificates installed, remove them or pick another
  host; a MITM'd TLS path is not acceptable evidence.

### 4.11 Disk free space

- Run footprint is small (dorm DB ~0.5 MB after 20 min; expect <100 MB for
  24h). Require >=5 GB free as margin for logs and artifacts.

### 4.12 Sleep / hibernate disabled for the test duration

- Windows: `powercfg /change standby-timeout-ac 0`,
  `powercfg /change hibernate-timeout-ac 0`; confirm with `powercfg /a`.
- Linux: `sudo systemctl mask sleep.target suspend.target hibernate.target
  hybrid-sleep.target`; on desktop environments also disable automatic suspend
  in the session settings. Verify `systemctl is-enabled sleep.target` is masked.

### 4.13 Machine reboot / update policy

- Windows: pause Windows Update for the window (Settings > Windows Update >
  Pause updates) and confirm no auto-restart is scheduled.
- Linux: stop unattended upgrades for the window (`sudo systemctl stop
  unattended-upgrades` / disable the apt timer) and check
  `systemctl list-timers` for anything that could reboot or cut the network.

### 4.14 Process independence

- The `start` subcommand launches a detached process (on the dorm host it
  outlives the launching terminal).
- On Linux over SSH, additionally wrap the launch in `tmux`,
  `systemd-run --scope`, or `nohup` so an SSH disconnect cannot signal the
  process; then log out, log back in, and confirm via `status`.

### 4.15 No credentials transferred

- Fresh clone only. Do not copy any `.env`, key, or secret from the dorm
  machine. Phase 4A uses public endpoints exclusively with zero exchange
  writes. If any credential file is found on the new host, delete it and
  restart from a clean clone.

## 5. Validation sequence on the fresh host (in order)

1. Full checklist above: every box green.
2. Targeted tests:
   `uv run pytest tests/test_websocket.py tests/test_phase_4a_soak.py tests/test_public_network.py tests/test_vwap_shadow_soak.py`
3. Full quality gate (the same command CI runs on windows-latest and
   ubuntu-latest): `uv run python scripts/quality_gate.py`

## 6. Short shakedown (180-300s, required before any 24h attempt)

```bash
uv run python scripts/phase_4a_soak.py start --duration-seconds 300 --sample-interval-seconds 60 --network-mode direct --startup-wait-seconds 90
uv run python scripts/phase_4a_soak.py status  --artifact-dir artifacts/soak/phase_4a/<SHAKEDOWN_ID>
uv run python scripts/phase_4a_soak.py finalize --artifact-dir artifacts/soak/phase_4a/<SHAKEDOWN_ID>
```

(Substitute `--network-mode proxy --proxy-url <URL>` if direct is blocked, per
section 4.9; record the choice.)

Acceptance — all must hold (the final report exposes `shakedown_passed`):

- REST bootstrap PASS (`startup_evidence.rest_bootstrap = true`)
- public WS PASS (`startup_evidence.public_ws_connected = true`)
- heartbeat PASS (`last_heartbeat_utc` advances across samples)
- DB WAL + synchronous FULL (`journal_mode = wal`, `synchronous = 2`)
- integrity PASS (`quick_check = ok`, `integrity = ok`)
- exchange writes = 0 (`write_guard` all-zero, `exchange_write_attempts = 0`)
- graceful shutdown PASS (`graceful_shutdown = true`, duration target reached)

## 7. Formal 24h (only after shakedown PASS)

Frozen parameters, identical to the authorized dorm attempt:
BTC-USDT / 1h / vwap_window=24 / buy_deviation_bps=100 / duration=86400 /
sample_interval=300.

```bash
uv run python scripts/phase_4a_soak.py start --duration-seconds 86400 --sample-interval-seconds 300 --network-mode direct --startup-wait-seconds 90
```

- Single process, single continuous run, duration >=86400, sample interval 300.
- No stitching of any kind. Any interruption or failure means the 24h
  acceptance must be re-attempted from scratch under new authorization.
