"""Shared synthetic migration fixtures.

The default test suite must run from a fresh clone without any runtime
``data/`` files, so migration tests build a deterministic synthetic v21
database instead of depending on a historical production backup copy.
All identifiers below are synthetic and carry no real account, order or
credential material.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.storage.migrations import MIGRATIONS, MigrationManager

V21 = 21


def _now() -> str:
    return datetime.now(UTC).isoformat()


def build_synthetic_v21_database(path: Path) -> Path:
    """Create a deterministic v21 database with synthetic business rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise AssertionError(f"refusing to overwrite existing fixture target: {path}")
    manager = MigrationManager(path)
    manager.migrate(backup=False, target_version=V21)
    assert manager.status().current_version == V21
    _insert_synthetic_business_rows(path)
    assert manager.status().current_version == V21
    return path


def _insert_synthetic_business_rows(path: Path) -> None:
    now = _now()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO signals(signal_id, action, instrument_id, timestamp, reason,
            confidence, metadata_json) VALUES ('sig-fixture-0001', 'buy', 'BTC-USDT', ?,
            'synthetic-fixture', 'high', '{"origin": "migration-fixture"}')""",
            (now,),
        )
        connection.execute(
            """INSERT INTO orders(client_order_id, exchange_order_id, instrument_id, side,
            order_type, quantity, price, signal_id, state, filled_quantity, average_price,
            created_at, updated_at, run_id, mode, strategy_name, bar, order_source)
            VALUES ('clord-fixture-0001', 'xchg-fixture-0001', 'BTC-USDT', 'buy', 'limit',
            '0.001', '60000', 'sig-fixture-0001', 'filled', '0.001', '60000', ?, ?, '',
            'demo', 'fixture', '1h', 'synthetic')""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO fills(client_order_id, side, quantity, price, fee, filled_at,
            fill_id, exchange_fill_id, fee_currency, data_quality_status, source, mode, run_id)
            VALUES ('clord-fixture-0001', 'buy', '0.001', '60000', '0.01', ?, 'fill-fixture-0001',
            'trad-fixture-0001', 'USDT', 'trusted', 'synthetic', 'demo', '')""",
            (now,),
        )
        connection.execute(
            """INSERT INTO demo_order_proposals(proposal_id, proposal_version, run_id, source,
            strategy_name, instrument_id, instrument_type, trade_mode, side, order_type,
            planned_limit_price, requested_notional, approved_notional, quantity, estimated_fee,
            instrument_rule_snapshot_id, account_snapshot_id, reconciliation_snapshot_id,
            capability_audit_id, risk_decision_id, client_order_id, proposal_hash, status,
            blockers_json, warnings_json, created_at, expires_at)
            VALUES ('prop-fixture-0001', 1, 'run-fixture-0001', 'synthetic-fixture', 'fixture',
            'BTC-USDT', 'SPOT', 'cash', 'buy', 'limit', '60000', '60', '60', '0.001', '0.01',
            'rule-fixture', 'acct-fixture', 'recon-fixture', 'audit-fixture', 'risk-fixture',
            'clord-fixture-0001', 'hash-fixture', 'blocked', '[]', '[]', ?, ?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO continuous_demo_runs(run_id, strategy_name, instrument_id, timeframe,
            status, mode, configuration_hash, started_at, reconciliation_status)
            VALUES ('run-fixture-0001', 'fixture', 'BTC-USDT', '1h', 'stopped', 'demo',
            'fixture-hash', ?, 'not_started')""",
            (now,),
        )
        connection.execute(
            """INSERT INTO private_state_snapshots(scope_key, event_kind, event_time,
            normalized_json, payload_hash, needs_reconciliation, received_at)
            VALUES ('account/BTC-USDT', 'account', ?, '{"synthetic": true}',
            'fixture-payload-hash', 0, ?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO runtime_generations(generation_id, generation_number, status,
            created_at, activated_at, manifest_sha256, database_sha256_before,
            authorization_json, notes)
            VALUES ('gen-fixture-0001', 1, 'active', ?, ?, 'fixture-manifest-sha',
            'fixture-database-sha', '{"operator": "synthetic-fixture"}',
            'synthetic fixture generation for shadow replay compatibility')""",
            (now, now),
        )
        connection.commit()


def synthetic_v21_sha256(path: Path) -> str:
    from app.storage.database_backup import file_identity

    return file_identity(path).sha256


__all__ = ["MIGRATIONS", "V21", "build_synthetic_v21_database", "synthetic_v21_sha256"]
