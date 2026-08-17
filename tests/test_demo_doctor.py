from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

import app.services.demo_doctor as doctor_module
from app.config.run_config import RunConfig
from app.config.settings import Settings, TradingMode
from app.domain.market import Instrument
from app.services.demo_doctor import CheckStatus, DemoDoctor
from app.storage.database import Database
from tests.conftest import make_instrument


class PublicOnlyClient:
    public_calls = 0
    private_calls = 0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_server_time(self) -> int:
        type(self).public_calls += 1
        return 1

    def get_instrument(self, instrument_id: str) -> Instrument:
        type(self).public_calls += 1
        return make_instrument(instrument_id, "BTC", "USDT", "0.00001", "0.1")

    def close(self) -> None:
        return None


def test_doctor_checks_public_api_without_private_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'doctor.db'}"
    Database(database_url).initialize()
    settings = Settings(
        trading_mode=TradingMode.DEMO,
        database_url=database_url,
        okx_api_key=SecretStr(""),
        okx_secret_key=SecretStr(""),
        okx_passphrase=SecretStr(""),
    )
    config = RunConfig(mode=TradingMode.DEMO)
    monkeypatch.setattr(doctor_module, "OkxClient", PublicOnlyClient)

    report = DemoDoctor(config, settings).run()
    statuses = {check.name: check.status for check in report.checks}
    assert PublicOnlyClient.public_calls == 2
    assert PublicOnlyClient.private_calls == 0
    assert statuses["public_api"] is CheckStatus.PASS
    assert statuses["credentials"] is CheckStatus.BLOCKED
    assert statuses["private_api"] is CheckStatus.SKIPPED
    assert not report.order_submission_allowed
    assert report.exit_code == 2
