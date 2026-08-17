from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class MigrationError(RuntimeError):
    pass


_PROTECTED_PRODUCTION_DATABASE = (
    Path(__file__).resolve().parents[2] / "data" / "trading.db"
).resolve()


MigrationAction = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    signature: str
    apply: MigrationAction

    @property
    def checksum(self) -> str:
        source = f"{self.version}:{self.name}:{self.signature}"
        return hashlib.sha256(source.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    current_version: int
    target_version: int
    pending: tuple[str, ...]
    failed: tuple[str, ...]
    compatible: bool


BASELINE_TABLES = frozenset(
    {
        "candle_metadata",
        "signals",
        "risk_decisions",
        "orders",
        "order_state_changes",
        "fills",
        "portfolio_snapshots",
        "backtest_runs",
        "runtime_state",
        "system_events",
        "audit_records",
    }
)


def _execute_all(connection: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    for statement in statements:
        connection.execute(statement)


BASELINE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS candle_metadata (
        id INTEGER PRIMARY KEY, instrument_id TEXT NOT NULL, bar TEXT NOT NULL,
        first_timestamp TEXT NOT NULL, last_timestamp TEXT NOT NULL,
        row_count INTEGER NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY, action TEXT NOT NULL, instrument_id TEXT NOT NULL,
        timestamp TEXT NOT NULL, reason TEXT NOT NULL, confidence TEXT NOT NULL,
        metadata_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS risk_decisions (
        id INTEGER PRIMARY KEY, signal_id TEXT NOT NULL, allowed INTEGER NOT NULL,
        reason TEXT NOT NULL, adjusted_quantity TEXT NOT NULL,
        adjusted_notional TEXT NOT NULL, snapshot_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        client_order_id TEXT PRIMARY KEY, exchange_order_id TEXT,
        instrument_id TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL,
        quantity TEXT NOT NULL, price TEXT NOT NULL, signal_id TEXT NOT NULL,
        state TEXT NOT NULL, filled_quantity TEXT NOT NULL, average_price TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS order_state_changes (
        id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL, state TEXT NOT NULL,
        changed_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fills (
        id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL, side TEXT NOT NULL,
        quantity TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
        filled_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, mode TEXT NOT NULL,
        strategy_name TEXT NOT NULL, instrument_id TEXT NOT NULL, bar TEXT NOT NULL,
        balances_json TEXT NOT NULL, positions_json TEXT NOT NULL,
        average_entry_prices_json TEXT NOT NULL, equity TEXT NOT NULL,
        captured_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_instrument_time
        ON portfolio_snapshots (instrument_id, captured_at)""",
    """CREATE TABLE IF NOT EXISTS backtest_runs (
        run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
        initial_capital TEXT NOT NULL, final_equity TEXT NOT NULL,
        summary_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS runtime_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS system_events (
        id INTEGER PRIMARY KEY, event_type TEXT NOT NULL, message TEXT NOT NULL,
        details_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_records (
        id INTEGER PRIMARY KEY, record_type TEXT NOT NULL, run_id TEXT NOT NULL,
        mode TEXT NOT NULL, strategy_name TEXT NOT NULL,
        instrument_id TEXT NOT NULL, bar TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_audit_records_run
        ON audit_records (run_id, record_type)""",
)


def _baseline(connection: sqlite3.Connection) -> None:
    _execute_all(connection, BASELINE_STATEMENTS)


V2_STATEMENTS = (
    "runs",
    "instrument_snapshots",
    "dataset_snapshots",
    "processed_events",
    "legacy_tables",
    "orders.run_id/mode/strategy_name/bar",
    "fills.fill_id/exchange_fill_id",
)


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _column_names(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _v2_core(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, strategy_name TEXT NOT NULL,
            instrument_id TEXT NOT NULL, bar TEXT NOT NULL, status TEXT NOT NULL,
            app_version TEXT NOT NULL, git_commit TEXT NOT NULL,
            git_dirty INTEGER NOT NULL, config_hash TEXT NOT NULL,
            data_hash TEXT NOT NULL, instrument_snapshot_hash TEXT NOT NULL,
            seed INTEGER NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
            candle_count INTEGER NOT NULL, cost_parameters_json TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS instrument_snapshots (
            snapshot_hash TEXT PRIMARY KEY, instrument_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL, source TEXT NOT NULL, schema_version INTEGER NOT NULL,
            raw_json TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS dataset_snapshots (
            data_hash TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, bar TEXT NOT NULL,
            first_timestamp TEXT NOT NULL, last_timestamp TEXT NOT NULL,
            candle_count INTEGER NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS processed_events (
            idempotency_key TEXT PRIMARY KEY, event_type TEXT NOT NULL,
            payload_hash TEXT NOT NULL, processed_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS legacy_tables (
            table_name TEXT PRIMARY KEY, reason TEXT NOT NULL, marked_at TEXT NOT NULL
        )"""
    )
    for name in ("run_id", "mode", "strategy_name", "bar"):
        _add_column(connection, "orders", name, "TEXT NOT NULL DEFAULT ''")
    _add_column(connection, "fills", "fill_id", "TEXT")
    _add_column(connection, "fills", "exchange_fill_id", "TEXT")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_fill_id
        ON fills (fill_id) WHERE fill_id IS NOT NULL"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_exchange_fill_id
        ON fills (exchange_fill_id) WHERE exchange_fill_id IS NOT NULL"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_order_state_idempotency
        ON order_state_changes (client_order_id, state, changed_at)"""
    )
    now = datetime.now(UTC).isoformat()
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table in ("account_snapshots", "position_snapshots"):
        if table in tables:
            connection.execute(
                """INSERT OR IGNORE INTO legacy_tables(table_name, reason, marked_at)
                VALUES (?, ?, ?)""",
                (table, "v0.1 遗留表；保留历史数据，不再作为 v0.2 权威状态", now),
            )


V3_STATEMENTS = (
    "orders.order_source",
    "order_state_changes.audit_dimensions",
    "fills.fee_currency",
    "portfolio_snapshots.asset_balances/position_costs",
    "private_state_snapshots",
)


def _v3_demo_order_closure(connection: sqlite3.Connection) -> None:
    _add_column(connection, "orders", "order_source", "TEXT NOT NULL DEFAULT 'legacy'")
    for name in (
        "run_id",
        "mode",
        "strategy_name",
        "instrument_id",
        "bar",
        "signal_id",
        "exchange_order_id",
        "order_source",
    ):
        _add_column(connection, "order_state_changes", name, "TEXT NOT NULL DEFAULT ''")
    _add_column(connection, "fills", "fee_currency", "TEXT")
    _add_column(
        connection,
        "portfolio_snapshots",
        "asset_balances_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _add_column(
        connection,
        "portfolio_snapshots",
        "position_costs_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS private_state_snapshots (
            scope_key TEXT PRIMARY KEY, event_kind TEXT NOT NULL,
            event_time TEXT NOT NULL, normalized_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL, needs_reconciliation INTEGER NOT NULL,
            received_at TEXT NOT NULL, confirmed_at TEXT
        )"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_private_state_reconciliation
        ON private_state_snapshots (needs_reconciliation, event_time)"""
    )


MIGRATIONS = (
    Migration(1, "baseline_v0_1", "\n".join(BASELINE_STATEMENTS), _baseline),
    Migration(2, "unified_engine_v0_2", "\n".join(V2_STATEMENTS), _v2_core),
    Migration(
        3,
        "demo_order_closure_v0_3",
        "\n".join(V3_STATEMENTS),
        _v3_demo_order_closure,
    ),
    Migration(
        4,
        "account_field_contract_v0_4",
        "account model versioning and fill quarantine",
        lambda connection: _v4_account_model(connection),
    ),
    Migration(
        5,
        "managed_portfolio_equity_v0_5",
        "separate managed portfolio valuation from OKX account equity",
        lambda connection: _v5_managed_portfolio_equity(connection),
    ),
    Migration(
        6,
        "controlled_demo_order_proposals_v0_6",
        "dedicated proposal persistence and immutable proposal event audit",
        lambda connection: _v6_controlled_demo_order_proposals(connection),
    ),
    Migration(
        7,
        "unknown_order_recovery_v0_7",
        "read-only recovery evidence",
        lambda connection: _v7_unknown_order_recovery(connection),
    ),
    Migration(
        8,
        "recovery_endpoint_contract_v0_8",
        "endpoint contract and coverage audit",
        lambda connection: _v8_recovery_endpoint_contract(connection),
    ),
    Migration(
        9,
        "continuous_demo_v0_9",
        "continuous demo runs and managed inventory",
        lambda connection: _v9_continuous_demo(connection),
    ),
    Migration(
        10,
        "shadow_demo_v0_10",
        "shadow proposals and durable candle state",
        lambda connection: _v10_shadow_demo(connection),
    ),
    Migration(
        11,
        "shadow_safety_closure_v0_11",
        "durable runtime state, leases, shadow inventory and audit",
        lambda connection: _v11_shadow_safety(connection),
    ),
    Migration(
        12,
        "shadow_reconciliation_circuit_v0_12",
        "reconciliation audit and circuit breaker state",
        lambda connection: _v12_shadow_reconciliation(connection),
    ),
    Migration(
        13,
        "shadow_external_activity_v0_13",
        "external activity and recovery audit",
        lambda connection: _v13_external_activity(connection),
    ),
    Migration(
        14,
        "shadow_account_baseline_v0_14",
        "immutable shadow account baseline and task events",
        lambda connection: _v14_account_baseline(connection),
    ),
    Migration(
        15,
        "bounded_demo_v0_15",
        "bounded demo budget and submission audit",
        lambda connection: _v15_bounded_demo(connection),
    ),
    Migration(
        16,
        "bounded_order_linkage_v0_16",
        "signal proposal order linkage",
        lambda connection: _v16_bounded_order_linkage(connection),
    ),
    Migration(
        17,
        "vwap_strategy_state_v0_17",
        "durable entry stop and holding state",
        lambda connection: _v17_vwap_strategy_state(connection),
    ),
    Migration(
        18,
        "administrative_stale_closure_v0_18",
        "auditable close of stale runs missing immutable historical baselines",
        lambda connection: _v18_administrative_stale_closure(connection),
    ),
    Migration(
        19,
        "runtime_generation_and_legacy_quarantine_v0_19",
        "runtime generations, immutable legacy quarantine records and drift evidence",
        lambda connection: _v19_runtime_generation_and_legacy_quarantine(connection),
    ),
    Migration(
        20,
        "manual_validation_reconciliation_v0_20",
        "immutable manual-validation corrections, inventory events and evidence coverage windows",
        lambda connection: _v20_manual_validation_reconciliation(connection),
    ),
    Migration(
        21,
        "legacy_strategy_inventory_cleanup_v0_21",
        "authorized administrative cleanup runs, fills and immutable generation eligibility overlays",
        lambda connection: _v21_legacy_strategy_inventory_cleanup(connection),
    ),
    Migration(
        22,
        "private_state_submission_fence_v0_22",
        "versioned private-state snapshots and proposal submission fences",
        lambda connection: _v22_private_state_submission_fence(connection),
    ),
    Migration(
        23,
        "private_state_watermark_replay_v0_23",
        "persisted websocket watermark for private-state reconciliation replay",
        lambda connection: _v23_private_state_watermark_replay(connection),
    ),
)


def _v17_vwap_strategy_state(connection: sqlite3.Connection) -> None:
    _add_column(connection, "strategy_runtime_states", "state_json", "TEXT NOT NULL DEFAULT '{}'")


def _v18_administrative_stale_closure(connection: sqlite3.Connection) -> None:
    for name, definition in (
        ("closure_type", "TEXT"),
        ("historical_baseline_available", "INTEGER"),
        ("historical_balance_reconciliation_possible", "INTEGER"),
        ("closure_limitations", "TEXT"),
        ("evidence_level", "TEXT"),
    ):
        _add_column(connection, "continuous_run_recoveries", name, definition)


def _v19_runtime_generation_and_legacy_quarantine(connection: sqlite3.Connection) -> None:
    _add_column(connection, "continuous_demo_runs", "generation_id", "TEXT")
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS runtime_generations (
                generation_id TEXT PRIMARY KEY, generation_number INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK (status IN ('preparing','active','retired','failed')),
                created_at TEXT NOT NULL, activated_at TEXT, retired_at TEXT,
                manifest_sha256 TEXT NOT NULL, database_sha256_before TEXT NOT NULL,
                authorization_json TEXT NOT NULL, notes TEXT NOT NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_generations_single_active
            ON runtime_generations(status) WHERE status='active'""",
            """CREATE TABLE IF NOT EXISTS legacy_run_quarantines (
                quarantine_id TEXT PRIMARY KEY, legacy_run_id TEXT NOT NULL UNIQUE,
                generation_id TEXT NOT NULL, quarantined_at TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
                original_status TEXT NOT NULL, heartbeat_state TEXT NOT NULL,
                assessment_json TEXT NOT NULL, exchange_verification_json TEXT NOT NULL,
                operator_authorization_json TEXT NOT NULL,
                FOREIGN KEY (generation_id) REFERENCES runtime_generations(generation_id)
            )""",
            """CREATE TABLE IF NOT EXISTS legacy_lock_quarantines (
                quarantine_id TEXT PRIMARY KEY, lock_name TEXT NOT NULL, legacy_run_id TEXT NOT NULL UNIQUE,
                generation_id TEXT NOT NULL, quarantined_at TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL, original_lock_json TEXT NOT NULL,
                assessment_json TEXT NOT NULL,
                FOREIGN KEY (generation_id) REFERENCES runtime_generations(generation_id)
            )""",
            """CREATE TABLE IF NOT EXISTS generation_drift_events (
                event_id INTEGER PRIMARY KEY, generation_id TEXT NOT NULL, legacy_run_id TEXT NOT NULL,
                event_type TEXT NOT NULL, detected_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
                severity TEXT NOT NULL CHECK (severity IN ('info','warning','blocker')),
                FOREIGN KEY (generation_id) REFERENCES runtime_generations(generation_id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_generation_drift_events_generation
            ON generation_drift_events(generation_id, severity)""",
        ),
    )


def _v20_manual_validation_reconciliation(connection: sqlite3.Connection) -> None:
    """Add append-only evidence needed to correct historical manual validation data."""
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS reconciliation_batches (
                batch_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_evidence_hash TEXT NOT NULL,
                created_at TEXT NOT NULL, created_by TEXT NOT NULL, schema_version INTEGER NOT NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_reconciliation_batches_kind_evidence
            ON reconciliation_batches(kind, source_evidence_hash)""",
            """CREATE TABLE IF NOT EXISTS inventory_scope_corrections (
                correction_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                original_scope TEXT NOT NULL, corrected_scope TEXT NOT NULL,
                reason_code TEXT NOT NULL, supporting_order_id TEXT NOT NULL,
                supporting_trade_id TEXT NOT NULL, source_evidence_hash TEXT NOT NULL,
                created_at TEXT NOT NULL, created_by TEXT NOT NULL, schema_version INTEGER NOT NULL,
                UNIQUE(entity_type, entity_id, reason_code),
                FOREIGN KEY(batch_id) REFERENCES reconciliation_batches(batch_id)
            )""",
            """CREATE TABLE IF NOT EXISTS inventory_reconciliation_events (
                event_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                strategy_name TEXT NOT NULL, run_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
                inventory_scope TEXT NOT NULL, event_type TEXT NOT NULL,
                gross_base_filled TEXT NOT NULL, base_fee TEXT NOT NULL, net_base_delta TEXT NOT NULL,
                gross_quote_spent TEXT NOT NULL, quote_fee TEXT NOT NULL, net_quote_delta TEXT NOT NULL,
                source_order_id TEXT NOT NULL, source_trade_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, source_evidence_hash TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES reconciliation_batches(batch_id)
            )""",
            """CREATE TABLE IF NOT EXISTS evidence_coverage_windows (
                run_id TEXT PRIMARY KEY, run_start_at TEXT NOT NULL,
                historical_terminal_time TEXT, historical_terminal_time_known INTEGER NOT NULL,
                evidence_cutoff_at TEXT NOT NULL, coverage_start_at TEXT NOT NULL,
                coverage_end_at TEXT NOT NULL, coverage_end_source TEXT NOT NULL,
                query_completed_at TEXT NOT NULL, orders_coverage TEXT NOT NULL,
                fills_coverage TEXT NOT NULL, current_open_order_check TEXT NOT NULL,
                current_position_check TEXT NOT NULL, process_absence_check TEXT NOT NULL,
                heartbeat_check TEXT NOT NULL, lock_check TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL, limitations TEXT NOT NULL
            )""",
        ),
    )


def _v21_legacy_strategy_inventory_cleanup(connection: sqlite3.Connection) -> None:
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS legacy_inventory_cleanup_runs (
                cleanup_run_id TEXT PRIMARY KEY, cleanup_type TEXT NOT NULL,
                authorized_source_run_id TEXT NOT NULL, authorized_source_order_id TEXT NOT NULL,
                authorized_source_trade_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side='sell'), inventory_scope TEXT NOT NULL,
                generation_id TEXT NOT NULL, user_authorized INTEGER NOT NULL,
                status TEXT NOT NULL, original_quantity TEXT NOT NULL,
                base_fee_consumed TEXT NOT NULL, initial_eligible_quantity TEXT NOT NULL,
                quantized_cleanup_quantity TEXT NOT NULL, unsellable_dust_quantity TEXT NOT NULL,
                instrument_spec_hash TEXT NOT NULL, submission_budget INTEGER NOT NULL,
                submissions_consumed INTEGER NOT NULL DEFAULT 0, cancel_budget INTEGER NOT NULL,
                cancellations_consumed INTEGER NOT NULL DEFAULT 0, current_client_order_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (generation_id) REFERENCES runtime_generations(generation_id)
            )""",
            """CREATE TABLE IF NOT EXISTS legacy_inventory_cleanup_orders (
                cleanup_run_id TEXT NOT NULL, client_order_id TEXT NOT NULL UNIQUE,
                exchange_order_id TEXT, submission_sequence INTEGER NOT NULL,
                side TEXT NOT NULL CHECK (side='sell'), order_type TEXT NOT NULL CHECK (order_type='limit'),
                quantity TEXT NOT NULL, price TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (cleanup_run_id, submission_sequence),
                FOREIGN KEY (cleanup_run_id) REFERENCES legacy_inventory_cleanup_runs(cleanup_run_id)
            )""",
            """CREATE TABLE IF NOT EXISTS legacy_inventory_cleanup_fills (
                exchange_trade_id TEXT PRIMARY KEY, cleanup_run_id TEXT NOT NULL,
                exchange_order_id TEXT NOT NULL, client_order_id TEXT NOT NULL,
                quantity TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
                fee_currency TEXT, filled_at TEXT NOT NULL,
                FOREIGN KEY (cleanup_run_id) REFERENCES legacy_inventory_cleanup_runs(cleanup_run_id)
            )""",
            """CREATE TABLE IF NOT EXISTS inventory_generation_eligibility_overlays (
                overlay_id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL,
                source_order_id TEXT NOT NULL, source_trade_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL, original_scope TEXT NOT NULL,
                effective_scope TEXT NOT NULL, classification TEXT NOT NULL,
                quantity TEXT NOT NULL, eligible_for_active_generation INTEGER NOT NULL,
                automatic_sell_allowed INTEGER NOT NULL, strategy_sell_allowed INTEGER NOT NULL,
                instrument_spec_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(source_run_id, source_order_id, source_trade_id, classification)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_cleanup_runs_source
            ON legacy_inventory_cleanup_runs(authorized_source_run_id, status)""",
        ),
    )


def _v22_private_state_submission_fence(connection: sqlite3.Connection) -> None:
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS private_state_control (
                control_id INTEGER PRIMARY KEY CHECK (control_id=1), epoch INTEGER NOT NULL,
                version INTEGER NOT NULL, status TEXT NOT NULL,
                last_consistent_at TEXT, last_event_at TEXT, dirty_reasons_json TEXT NOT NULL,
                unknown_order_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            )""",
            """INSERT OR IGNORE INTO private_state_control
            (control_id,epoch,version,status,last_consistent_at,last_event_at,dirty_reasons_json,unknown_order_count,updated_at)
            VALUES (1,1,0,'bootstrapping',NULL,NULL,'{"items":[]}',0,CURRENT_TIMESTAMP)""",
        ),
    )
    for name, definition in (
        ("private_state_epoch", "INTEGER NOT NULL DEFAULT 1"),
        ("private_state_version", "INTEGER NOT NULL DEFAULT 0"),
        ("fenced_private_state_version", "INTEGER"),
        ("fenced_at", "TEXT"),
    ):
        _add_column(connection, "demo_order_proposals", name, definition)


def _v23_private_state_watermark_replay(connection: sqlite3.Connection) -> None:
    _add_column(connection, "private_state_control", "ws_watermark", "INTEGER NOT NULL DEFAULT 0")


def _v4_account_model(connection: sqlite3.Connection) -> None:
    _add_column(
        connection,
        "portfolio_snapshots",
        "balance_model_version",
        "TEXT NOT NULL DEFAULT 'legacy_untrusted'",
    )
    _add_column(
        connection,
        "portfolio_snapshots",
        "field_contract_version",
        "TEXT NOT NULL DEFAULT 'legacy'",
    )
    _add_column(
        connection,
        "portfolio_snapshots",
        "account_configuration_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _add_column(
        connection, "portfolio_snapshots", "account_equity_json", "TEXT NOT NULL DEFAULT '{}'"
    )
    _add_column(connection, "fills", "data_quality_status", "TEXT NOT NULL DEFAULT 'trusted'")
    _add_column(connection, "fills", "quarantine_reason", "TEXT")
    _add_column(connection, "fills", "eligible_for_cost_basis", "INTEGER NOT NULL DEFAULT 1")
    _add_column(connection, "fills", "source", "TEXT NOT NULL DEFAULT 'legacy'")
    _add_column(connection, "fills", "mode", "TEXT NOT NULL DEFAULT ''")
    _add_column(connection, "fills", "run_id", "TEXT NOT NULL DEFAULT ''")
    connection.execute("""UPDATE fills SET data_quality_status='quarantined',
        quarantine_reason='missing_parent_order', eligible_for_cost_basis=0
        WHERE client_order_id NOT IN (SELECT client_order_id FROM orders)""")
    connection.execute("""CREATE INDEX IF NOT EXISTS idx_fills_cost_basis
        ON fills (eligible_for_cost_basis, mode, client_order_id)""")


def _v5_managed_portfolio_equity(connection: sqlite3.Connection) -> None:
    _add_column(connection, "portfolio_snapshots", "managed_equity", "TEXT")
    connection.execute(
        """UPDATE portfolio_snapshots SET managed_equity = equity
        WHERE managed_equity IS NULL"""
    )


def _v6_controlled_demo_order_proposals(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS demo_order_proposals (
            proposal_id TEXT PRIMARY KEY, proposal_version INTEGER NOT NULL,
            run_id TEXT NOT NULL, source TEXT NOT NULL, strategy_name TEXT NOT NULL,
            instrument_id TEXT NOT NULL, instrument_type TEXT NOT NULL,
            trade_mode TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL,
            reference_bid TEXT, reference_ask TEXT, reference_last TEXT,
            planned_limit_price TEXT NOT NULL, requested_notional TEXT NOT NULL,
            approved_notional TEXT NOT NULL, quantity TEXT NOT NULL, estimated_fee TEXT NOT NULL,
            instrument_rule_snapshot_id TEXT NOT NULL, account_snapshot_id TEXT NOT NULL,
            reconciliation_snapshot_id TEXT NOT NULL, capability_audit_id TEXT NOT NULL,
            risk_decision_id TEXT NOT NULL, client_order_id TEXT NOT NULL UNIQUE,
            proposal_hash TEXT NOT NULL, status TEXT NOT NULL, blockers_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            invalidated_at TEXT, invalidation_reason TEXT,
            restart_revalidation_required INTEGER NOT NULL DEFAULT 1,
            submission_performed INTEGER NOT NULL DEFAULT 0,
            submitted_at TEXT, exchange_order_id TEXT
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS demo_order_proposal_events (
            event_id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL,
            event_type TEXT NOT NULL, event_time TEXT NOT NULL,
            previous_status TEXT, new_status TEXT, reason TEXT NOT NULL,
            details_json TEXT NOT NULL,
            FOREIGN KEY(proposal_id) REFERENCES demo_order_proposals(proposal_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_demo_order_proposal_events_proposal_time "
        "ON demo_order_proposal_events(proposal_id, event_time)"
    )


def _v7_unknown_order_recovery(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS unknown_order_recoveries (
        recovery_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, local_order_id TEXT NOT NULL,
        original_client_order_id TEXT NOT NULL, status TEXT NOT NULL, confidence TEXT NOT NULL,
        exchange_order_id TEXT, blockers_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
        created_at TEXT NOT NULL, completed_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS unknown_order_recovery_queries (
        id INTEGER PRIMARY KEY, recovery_id TEXT NOT NULL, endpoint TEXT NOT NULL,
        begin_at TEXT NOT NULL, end_at TEXT NOT NULL, pages_read INTEGER NOT NULL,
        records_read INTEGER NOT NULL, completed INTEGER NOT NULL, http_status INTEGER,
        okx_code TEXT, error_classification TEXT, error_message TEXT,
        FOREIGN KEY(recovery_id) REFERENCES unknown_order_recoveries(recovery_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS unknown_order_recovery_candidates (
        id INTEGER PRIMARY KEY, recovery_id TEXT NOT NULL, source_endpoint TEXT NOT NULL,
        exchange_order_id TEXT, client_order_id TEXT, fingerprint_json TEXT NOT NULL,
        match_type TEXT NOT NULL, contradiction_json TEXT NOT NULL,
        FOREIGN KEY(recovery_id) REFERENCES unknown_order_recoveries(recovery_id)
    )""")


def _v8_recovery_endpoint_contract(connection: sqlite3.Connection) -> None:
    for name, definition in (
        ("contract_status", "TEXT NOT NULL DEFAULT 'officially_documented'"),
        ("applicability_status", "TEXT NOT NULL DEFAULT 'applicable'"),
        ("blocking", "INTEGER NOT NULL DEFAULT 1"),
        ("superseded_by", "TEXT"),
        ("first_record_time", "TEXT"),
        ("last_record_time", "TEXT"),
    ):
        _add_column(connection, "unknown_order_recovery_queries", name, definition)


def _v9_continuous_demo(connection: sqlite3.Connection) -> None:
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS continuous_demo_runs (run_id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL, instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL, configuration_hash TEXT NOT NULL, started_at TEXT NOT NULL, stopped_at TEXT, stop_reason TEXT, processed_candle_count INTEGER NOT NULL DEFAULT 0, generated_signal_count INTEGER NOT NULL DEFAULT 0, submitted_order_count INTEGER NOT NULL DEFAULT 0, unknown_order_count INTEGER NOT NULL DEFAULT 0, reconciliation_status TEXT NOT NULL, last_heartbeat_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS continuous_demo_run_events (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, event_type TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS processed_candles (run_id TEXT NOT NULL, instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, candle_open_time TEXT NOT NULL, candle_close_time TEXT NOT NULL, is_confirmed INTEGER NOT NULL, market_data_source TEXT NOT NULL, processed_at TEXT NOT NULL, strategy_version TEXT NOT NULL, PRIMARY KEY(run_id,instrument_id,timeframe,candle_open_time))""",
            """CREATE TABLE IF NOT EXISTS strategy_signal_events (signal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, instrument_id TEXT NOT NULL, candle_open_time TEXT NOT NULL, signal_type TEXT NOT NULL, signal_value TEXT NOT NULL, strategy_state_hash TEXT NOT NULL, position_state_hash TEXT NOT NULL, decision TEXT NOT NULL, blockers_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id,instrument_id,candle_open_time,strategy_state_hash))""",
            """CREATE TABLE IF NOT EXISTS managed_inventory (strategy_name TEXT NOT NULL, run_id TEXT NOT NULL, instrument_id TEXT NOT NULL, inventory_scope TEXT NOT NULL, acquired_quantity TEXT NOT NULL, disposed_quantity TEXT NOT NULL, reserved_quantity TEXT NOT NULL, average_cost TEXT, realized_pnl TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(strategy_name,run_id,instrument_id,inventory_scope))""",
            """CREATE TABLE IF NOT EXISTS trading_heartbeats (run_id TEXT PRIMARY KEY, process_id INTEGER NOT NULL, host_id TEXT NOT NULL, heartbeat_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL)""",
        ),
    )


def _v10_shadow_demo(connection: sqlite3.Connection) -> None:
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS shadow_order_proposals (shadow_proposal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, signal_id TEXT NOT NULL, instrument_id TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL, reference_price TEXT NOT NULL, planned_price TEXT NOT NULL, quantity TEXT NOT NULL, notional TEXT NOT NULL, estimated_fee TEXT NOT NULL, inventory_scope TEXT NOT NULL, blockers_json TEXT NOT NULL, warnings_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, submission_performed INTEGER NOT NULL DEFAULT 0, exchange_order_id TEXT)""",
            """CREATE TABLE IF NOT EXISTS strategy_signal_state (run_id TEXT PRIMARY KEY, strategy_name TEXT NOT NULL, instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, last_candle_open_time TEXT, previous_relation TEXT, last_signal_type TEXT, last_signal_candle_time TEXT, state_hash TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        ),
    )


def _v11_shadow_safety(connection: sqlite3.Connection) -> None:
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS strategy_runtime_states (
            state_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, strategy_name TEXT NOT NULL,
            strategy_version TEXT NOT NULL, instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL,
            last_candle_open_time TEXT, previous_fast_value TEXT, previous_slow_value TEXT,
            previous_relation TEXT, last_signal_type TEXT, last_signal_candle_time TEXT,
            warmup_completed INTEGER NOT NULL, warmup_candle_count INTEGER NOT NULL,
            state_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(run_id,strategy_name,instrument_id,timeframe)
        )""",
            """CREATE TABLE IF NOT EXISTS shadow_order_proposal_events (
            event_id INTEGER PRIMARY KEY, shadow_proposal_id TEXT NOT NULL,
            event_type TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
        )""",
            """CREATE TABLE IF NOT EXISTS shadow_managed_inventory (
            run_id TEXT NOT NULL, strategy_name TEXT NOT NULL, instrument_id TEXT NOT NULL,
            acquired_quantity TEXT NOT NULL DEFAULT '0', disposed_quantity TEXT NOT NULL DEFAULT '0',
            available_quantity TEXT NOT NULL DEFAULT '0', reserved_quantity TEXT NOT NULL DEFAULT '0',
            average_cost TEXT, realized_pnl TEXT NOT NULL DEFAULT '0', unrealized_pnl TEXT NOT NULL DEFAULT '0',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(run_id,strategy_name,instrument_id)
        )""",
            """CREATE TABLE IF NOT EXISTS shadow_inventory_events (
            event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, event_type TEXT NOT NULL,
            quantity TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
        )""",
            """CREATE TABLE IF NOT EXISTS continuous_run_locks (
            lock_name TEXT PRIMARY KEY, run_id TEXT NOT NULL, host_id TEXT NOT NULL,
            process_id INTEGER NOT NULL, acquired_at TEXT NOT NULL, last_renewed_at TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL, released_at TEXT, release_reason TEXT
        )""",
        ),
    )
    for name, definition in (
        ("last_market_message_at", "TEXT"),
        ("last_private_message_at", "TEXT"),
        ("last_reconciliation_at", "TEXT"),
        ("shadow_proposal_count", "INTEGER NOT NULL DEFAULT 0"),
        ("stop_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("private_stream_status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("public_stream_status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("circuit_breaker_status", "TEXT NOT NULL DEFAULT 'continue'"),
    ):
        _add_column(connection, "continuous_demo_runs", name, definition)
    for name, definition in (
        ("strategy_name", "TEXT NOT NULL DEFAULT 'moving_average_cross'"),
        ("strategy_version", "TEXT NOT NULL DEFAULT 'moving_average_cross_v1'"),
        ("timeframe", "TEXT NOT NULL DEFAULT '5m'"),
        ("candle_close_time", "TEXT"),
        ("previous_relation", "TEXT"),
        ("current_relation", "TEXT"),
        ("signal_type", "TEXT"),
        ("strategy_state_hash", "TEXT NOT NULL DEFAULT ''"),
        ("inventory_state_hash", "TEXT NOT NULL DEFAULT ''"),
        ("account_state_hash", "TEXT NOT NULL DEFAULT ''"),
        ("decision", "TEXT NOT NULL DEFAULT 'no_signal'"),
        ("warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        _add_column(connection, "strategy_signal_events", name, definition)
    for name, definition in (
        ("instrument_type", "TEXT NOT NULL DEFAULT 'SPOT'"),
        ("trade_mode", "TEXT NOT NULL DEFAULT 'cash'"),
        ("reference_bid", "TEXT"),
        ("reference_ask", "TEXT"),
        ("reference_last", "TEXT"),
        ("capability_status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("risk_status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("decision", "TEXT NOT NULL DEFAULT 'prepared'"),
        ("is_shadow", "INTEGER NOT NULL DEFAULT 1"),
    ):
        _add_column(connection, "shadow_order_proposals", name, definition)
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_signal_identity ON strategy_signal_events(run_id,strategy_name,instrument_id,timeframe,candle_open_time,signal_type)"
    )


def _v12_shadow_reconciliation(connection: sqlite3.Connection) -> None:
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS continuous_reconciliations (
            reconciliation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, completed_at TEXT NOT NULL, baseline_snapshot_id TEXT,
            current_snapshot_id TEXT, blockers_json TEXT NOT NULL, warnings_json TEXT NOT NULL
        )""",
            """CREATE TABLE IF NOT EXISTS continuous_reconciliation_checks (
            check_id INTEGER PRIMARY KEY, reconciliation_id TEXT NOT NULL, check_name TEXT NOT NULL,
            status TEXT NOT NULL, expected TEXT NOT NULL, actual TEXT NOT NULL, source TEXT NOT NULL,
            blocker INTEGER NOT NULL DEFAULT 0, warning INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            FOREIGN KEY(reconciliation_id) REFERENCES continuous_reconciliations(reconciliation_id)
        )""",
        ),
    )
    for name, definition in (
        ("initial_reconciliation_status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("reconciliation_count", "INTEGER NOT NULL DEFAULT 0"),
        ("reconciliation_failure_count", "INTEGER NOT NULL DEFAULT 0"),
        ("circuit_breaker_action", "TEXT NOT NULL DEFAULT 'continue'"),
        ("circuit_breaker_code", "TEXT"),
        ("circuit_breaker_severity", "TEXT"),
        ("circuit_breaker_triggered_at", "TEXT"),
        ("recovery_required", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(connection, "continuous_demo_runs", name, definition)


def _v13_external_activity(connection: sqlite3.Connection) -> None:
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS continuous_external_activities (
            activity_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, activity_type TEXT NOT NULL,
            exchange_order_id TEXT, exchange_trade_id TEXT, instrument_id TEXT, currency TEXT,
            baseline_value TEXT, current_value TEXT, difference TEXT, detected_at TEXT NOT NULL,
            source_endpoint TEXT NOT NULL, classification TEXT NOT NULL, severity TEXT NOT NULL,
            evidence_json TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0
        )""",
            """CREATE TABLE IF NOT EXISTS continuous_run_recoveries (
            recovery_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, completed_at TEXT NOT NULL, original_run_status TEXT NOT NULL,
            final_run_status TEXT NOT NULL, lock_status TEXT NOT NULL, database_status TEXT NOT NULL,
            reconciliation_status TEXT NOT NULL, external_activity_status TEXT NOT NULL,
            blockers_json TEXT NOT NULL, warnings_json TEXT NOT NULL
        )""",
            """CREATE TABLE IF NOT EXISTS continuous_run_recovery_checks (
            check_id INTEGER PRIMARY KEY, recovery_id TEXT NOT NULL, check_name TEXT NOT NULL,
            status TEXT NOT NULL, expected TEXT NOT NULL, actual TEXT NOT NULL,
            blocker INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        )""",
        ),
    )


def _v14_account_baseline(connection: sqlite3.Connection) -> None:
    _execute_all(
        connection,
        (
            """CREATE TABLE IF NOT EXISTS shadow_account_baselines (
            baseline_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, account_fingerprint TEXT NOT NULL,
            account_mode TEXT NOT NULL, position_mode TEXT NOT NULL, environment TEXT NOT NULL,
            btc_total TEXT NOT NULL, btc_available TEXT NOT NULL, btc_frozen TEXT NOT NULL,
            usdt_total TEXT NOT NULL, usdt_available TEXT NOT NULL, usdt_frozen TEXT NOT NULL,
            pending_order_ids_json TEXT NOT NULL, recent_order_ids_json TEXT NOT NULL,
            recent_trade_ids_json TEXT NOT NULL, latest_order_time TEXT, latest_fill_time TEXT,
            derivative_position_count INTEGER NOT NULL, liability_count INTEGER NOT NULL, captured_at TEXT NOT NULL
        )""",
            """CREATE TABLE IF NOT EXISTS continuous_task_events (
            event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, task_name TEXT NOT NULL,
            event_type TEXT NOT NULL, exception_class TEXT, sanitized_message TEXT,
            restart_count INTEGER NOT NULL DEFAULT 0, circuit_breaker_code TEXT,
            started_at TEXT NOT NULL, failed_at TEXT
        )""",
        ),
    )


def _v15_bounded_demo(connection: sqlite3.Connection) -> None:
    for name, definition in (
        ("submission_budget", "INTEGER NOT NULL DEFAULT 0"),
        ("submissions_reserved", "INTEGER NOT NULL DEFAULT 0"),
        ("maximum_notional_per_order", "TEXT NOT NULL DEFAULT '0'"),
        ("maximum_managed_exposure", "TEXT NOT NULL DEFAULT '0'"),
        ("maximum_open_orders", "INTEGER NOT NULL DEFAULT 0"),
        ("bounded_acceptance_status", "TEXT"),
    ):
        _add_column(connection, "continuous_demo_runs", name, definition)
    connection.execute("""CREATE TABLE IF NOT EXISTS bounded_submission_events (
        event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, proposal_id TEXT,
        slot_number INTEGER NOT NULL, event_type TEXT NOT NULL, details_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")


def _v16_bounded_order_linkage(connection: sqlite3.Connection) -> None:
    for name, definition in (
        ("signal_id", "TEXT NOT NULL DEFAULT ''"),
        ("candle_id", "TEXT NOT NULL DEFAULT ''"),
        ("acceptance_only", "INTEGER NOT NULL DEFAULT 0"),
        ("inventory_scope", "TEXT NOT NULL DEFAULT 'strategy_managed'"),
        ("submission_sequence", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(connection, "demo_order_proposals", name, definition)


class MigrationManager:
    def __init__(self, database_path: Path) -> None:
        self.path = database_path.resolve()

    def status(self) -> MigrationStatus:
        try:
            return self._read_status()
        except sqlite3.Error as exc:
            raise MigrationError(f"database migration status is unreadable: {exc}") from exc

    def _read_status(self) -> MigrationStatus:
        if not self.path.exists():
            return MigrationStatus(0, MIGRATIONS[-1].version, self._names_after(0), (), True)
        with self._connect() as connection:
            tables = self._tables(connection)
            if "schema_migrations" not in tables:
                compatible = not tables or BASELINE_TABLES.issubset(tables)
                return MigrationStatus(
                    0,
                    MIGRATIONS[-1].version,
                    tuple(migration.name for migration in MIGRATIONS),
                    (),
                    compatible,
                )
            successful = self._successful(connection)
            failed = tuple(
                str(row[0])
                for row in connection.execute(
                    """SELECT failed.name FROM schema_migrations AS failed
                    WHERE failed.execution_status = 'failed'
                    AND NOT EXISTS (
                        SELECT 1 FROM schema_migrations AS succeeded
                        WHERE succeeded.version = failed.version
                        AND succeeded.execution_status IN ('successful', 'adopted')
                    )
                    ORDER BY failed.id"""
                )
            )
            current = max(successful, default=0)
            compatible = self._history_is_valid(connection, successful)
            return MigrationStatus(
                current,
                MIGRATIONS[-1].version,
                self._names_after(current),
                failed,
                compatible,
            )

    def migrate(
        self,
        *,
        dry_run: bool = False,
        backup: bool = True,
        production_permit: object | None = None,
        target_version: int | None = None,
    ) -> tuple[str, ...]:
        try:
            return self._migrate(
                dry_run=dry_run,
                backup=backup,
                production_permit=production_permit,
                target_version=target_version,
            )
        except MigrationError:
            raise
        except sqlite3.Error as exc:
            raise MigrationError(f"database migration failed: {exc}") from exc

    def _migrate(
        self,
        *,
        dry_run: bool = False,
        backup: bool = True,
        production_permit: object | None = None,
        target_version: int | None = None,
    ) -> tuple[str, ...]:
        existing = self.path.exists() and self.path.stat().st_size > 0
        plan = self.status()
        if not plan.compatible:
            raise MigrationError("数据库结构或迁移校验和不兼容，已拒绝升级")
        target = MIGRATIONS[-1].version if target_version is None else target_version
        if target < plan.current_version:
            raise MigrationError("database migration downgrade is not supported")
        if target < 1 or target > MIGRATIONS[-1].version:
            raise MigrationError(f"unknown database migration target version: {target}")
        pending = tuple(
            migration.name
            for migration in MIGRATIONS
            if plan.current_version < migration.version <= target
        )
        if dry_run:
            return pending
        if existing and pending and self.path == _PROTECTED_PRODUCTION_DATABASE:
            from app.storage.migration_execution import _consume_production_migration_permit

            try:
                _consume_production_migration_permit(production_permit, self.path)
            except PermissionError as exc:
                raise MigrationError(
                    "正式数据库写迁移需要一次性授权执行入口，直接运行 db-migrate 被拒绝；"
                    "请显式设置 DATABASE_URL 指向数据库副本后运行同一命令"
                ) from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if existing and backup and pending:
            self.backup()
        with self._connect() as connection:
            self._ensure_history(connection)
            tables = self._tables(connection)
            if plan.current_version == 0 and BASELINE_TABLES.issubset(tables):
                self._record(connection, MIGRATIONS[0], "adopted")
                connection.commit()
        applied: list[str] = []
        for migration in MIGRATIONS:
            if migration.version > target:
                break
            with self._connect() as connection:
                self._ensure_history(connection)
                if self._is_successful(connection, migration.version):
                    continue
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    migration.apply(connection)
                    self._record(connection, migration, "successful")
                    connection.commit()
                    applied.append(migration.name)
                except Exception as exc:
                    connection.rollback()
                    try:
                        self._record(connection, migration, "failed")
                        connection.commit()
                    except sqlite3.Error as record_error:
                        raise MigrationError(
                            f"migration {migration.name} failed and failure recording failed: "
                            f"{record_error}"
                        ) from exc
                    raise MigrationError(f"迁移 {migration.name} 失败，事务已回滚: {exc}") from exc
        after = self.status()
        if not after.compatible or after.current_version != target:
            raise MigrationError("database migration did not reach the explicit target version")
        return tuple(applied)

    def backup(self, destination: Path | None = None) -> Path:
        if not self.path.exists():
            raise MigrationError("数据库不存在，无法备份")
        if destination is None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            destination = self.path.parent / "backups" / f"{self.path.stem}-{stamp}.db"
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination == self.path:
            raise MigrationError("备份目标不能覆盖原数据库")
        if destination.exists():
            raise MigrationError("备份目标已存在，拒绝覆盖")
        try:
            with sqlite3.connect(self.path) as source, sqlite3.connect(destination) as target:
                source.backup(target)
        except sqlite3.Error as exc:
            destination.unlink(missing_ok=True)
            raise MigrationError(f"数据库备份失败: {exc}") from exc
        return destination

    def require_current(self) -> None:
        status = self.status()
        if not status.compatible:
            raise MigrationError("数据库迁移校验失败")
        if status.pending:
            raise MigrationError("数据库尚未升级，请先运行 db-migrate")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _ensure_history(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                id INTEGER PRIMARY KEY, version INTEGER NOT NULL, name TEXT NOT NULL,
                checksum TEXT NOT NULL, applied_at TEXT NOT NULL,
                execution_status TEXT NOT NULL
                CHECK (execution_status IN ('successful', 'failed', 'adopted'))
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_schema_migrations_version
            ON schema_migrations(version, execution_status)"""
        )

    @staticmethod
    def _tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"""
            )
        }

    @staticmethod
    def _successful(connection: sqlite3.Connection) -> dict[int, sqlite3.Row]:
        return {
            int(row["version"]): row
            for row in connection.execute(
                """SELECT version, name, checksum FROM schema_migrations
                WHERE execution_status IN ('successful', 'adopted') ORDER BY id"""
            )
        }

    @staticmethod
    def _is_successful(connection: sqlite3.Connection, version: int) -> bool:
        row = connection.execute(
            """SELECT 1 FROM schema_migrations
            WHERE version = ? AND execution_status IN ('successful', 'adopted')
            LIMIT 1""",
            (version,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _record(connection: sqlite3.Connection, migration: Migration, status: str) -> None:
        connection.execute(
            """INSERT INTO schema_migrations
            (version, name, checksum, applied_at, execution_status)
            VALUES (?, ?, ?, ?, ?)""",
            (
                migration.version,
                migration.name,
                migration.checksum,
                datetime.now(UTC).isoformat(),
                status,
            ),
        )

    @staticmethod
    def _checksums_match(successful: dict[int, sqlite3.Row]) -> bool:
        expected = {migration.version: migration for migration in MIGRATIONS}
        if any(version not in expected for version in successful):
            return False
        return all(
            str(row["name"]) == expected[version].name
            and str(row["checksum"]) == expected[version].checksum
            for version, row in successful.items()
        )

    @classmethod
    def _history_is_valid(
        cls, connection: sqlite3.Connection, successful: dict[int, sqlite3.Row]
    ) -> bool:
        versions = [
            int(row[0])
            for row in connection.execute(
                """SELECT version FROM schema_migrations
                WHERE execution_status IN ('successful', 'adopted') ORDER BY id"""
            )
        ]
        if len(versions) != len(set(versions)):
            return False
        current = max(versions, default=0)
        if set(versions) != set(range(1, current + 1)):
            return False
        return cls._checksums_match(successful)

    @staticmethod
    def _names_after(version: int) -> tuple[str, ...]:
        return tuple(migration.name for migration in MIGRATIONS if migration.version > version)
