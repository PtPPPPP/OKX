from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.config.run_config import load_run_config
from app.domain.market import Candle
from app.market.historical_data import save_candles_csv
from app.services.legacy_quarantine import RuntimeGenerationService
from app.services.shadow_replay import run_shadow_replay
from app.storage.database import Database


def _candle(timestamp: datetime, price: str) -> Candle:
    value = Decimal(price)
    return Candle(
        timestamp,
        value,
        value + Decimal("1"),
        value - Decimal("1"),
        value,
        Decimal("10"),
        True,
    )


def test_vwap_shadow_replay_is_read_only_and_persists_only_shadow_proposals(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [_candle(start + timedelta(hours=index), "100") for index in range(24)]
    candles.append(_candle(start + timedelta(hours=24), "98"))
    candle_path = tmp_path / "vwap-shadow.csv"
    save_candles_csv(candles, candle_path)
    database = Database(f"sqlite:///{tmp_path / 'vwap-shadow.db'}")
    database.initialize()
    generation = RuntimeGenerationService(database, start)
    generation_id = generation.create_preparing(
        "manifest",
        "database",
        {"test": True},
        "test runtime",
    )
    generation.activate(generation_id)
    config = load_run_config(Path("configs/btc_vwap_shadow.yaml"), environ={})

    result = run_shadow_replay(database, config, candle_path, maximum=25)

    assert result["status"] == "stopped"
    assert result["processed_candles"] == 25
    assert result["broker_write_calls"] == 0
    assert result["shadow_proposals"] == 1
    with database.connect() as connection:
        run = connection.execute(
            """SELECT mode,status,submitted_order_count
            FROM continuous_demo_runs WHERE run_id=?""",
            (result["run_id"],),
        ).fetchone()
        proposal = connection.execute(
            """SELECT decision,is_shadow,submission_performed,blockers_json
            FROM shadow_order_proposals WHERE run_id=?""",
            (result["run_id"],),
        ).fetchone()
        production_proposals = connection.execute(
            "SELECT COUNT(*) FROM demo_order_proposals"
        ).fetchone()[0]

    assert tuple(run) == ("shadow", "stopped", 0)
    assert tuple(proposal) == ("blocked", 1, 0, '["shadow_only", "not_sized"]')
    assert production_proposals == 0
