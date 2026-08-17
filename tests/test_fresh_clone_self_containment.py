"""Fresh-clone invariant: the default test suite never needs runtime data/.

A fresh git clone (no ``data/``, no ``artifacts/``, no prior application run)
must be able to run the default test suite. This module guards that property
structurally: no test may load historical CSVs, database backups or backtest
artifacts from the runtime tree, and the tracked fixtures must stay
deterministic, sanitized and version-controlled.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_ROOT = _TESTS.parent
_VWAP_FIXTURES = _TESTS / "fixtures" / "vwap"

# References to the production database path are allowed only for tests that
# assert path-identity refusals or guarded immutability checks; they never
# require the file to exist.
_PRODUCTION_PATH_ALLOWLIST = {
    "test_database_backup_restore.py": "path-identity refusal checks only",
    "test_legacy_quarantine.py": "negative guard: constructing a service on the production path must fail",
    "test_vwap_shadow.py": "existence-guarded before/after immutability check",
    "test_vwap_shadow_soak.py": "existence-guarded before/after immutability check",
}

_FORBIDDEN_LITERALS = (
    "data/btc_usdt",
    "data/backups",
    "artifacts/backtests",
    "data/prospective",
    "data/market_information",
)

_FIXTURE_SHA256 = {
    "btc_usdt_1h_600.csv": ("d931461c41d28ab8748aa51a637488e08ddf86540c1303370f98545d0571960c"),
    "btc_usdt_1h_live.csv": ("472e4dc1c88a389bf26097b0390ecfd8d45c9cc96f7d468903d5ae80501843ed"),
    "vwap_baseline_v1_signals.csv": (
        "4bdffcd8dfabb5993c66c55121d303671a3623bc8834e370cec8aee002f8bb9e"
    ),
}

_SECRET_MARKERS = (
    "api_key",
    "api-key",
    "apikey",
    "secret_key",
    "secret-key",
    "passphrase",
    "private_key",
    "BEGIN RSA",
    "BEGIN OPENSSH",
)


def test_no_test_reads_runtime_data_or_artifacts() -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        for literal in _FORBIDDEN_LITERALS:
            if literal in text:
                offenders.append(f"{path.name}: {literal}")
        if "data/trading.db" in text and path.name not in _PRODUCTION_PATH_ALLOWLIST:
            offenders.append(f"{path.name}: data/trading.db")
    assert not offenders, f"tests depend on runtime data: {offenders}"


def test_tracked_vwap_fixtures_exist_with_pinned_hashes() -> None:
    for name, expected in _FIXTURE_SHA256.items():
        fixture = _VWAP_FIXTURES / name
        assert fixture.is_file(), f"missing tracked fixture: {fixture}"
        digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert digest == expected, f"fixture {name} changed: {digest}"


def test_tracked_fixtures_contain_no_secret_material() -> None:
    for fixture in sorted(_VWAP_FIXTURES.glob("*.csv")):
        text = fixture.read_text(encoding="utf-8").lower()
        for marker in _SECRET_MARKERS:
            assert marker.lower() not in text, f"{fixture.name} contains {marker}"
    for fixture in sorted((_TESTS / "fixtures" / "okx").glob("*.json")):
        text = fixture.read_text(encoding="utf-8").lower()
        for marker in _SECRET_MARKERS:
            assert marker.lower() not in text, f"{fixture.name} contains {marker}"


def test_runtime_data_tree_is_gitignored() -> None:
    gitignore = (_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("data/*.db", "data/**/*.csv", "data/**/*.jsonl", "artifacts/"):
        assert pattern in gitignore, f".gitignore lost runtime pattern: {pattern}"
