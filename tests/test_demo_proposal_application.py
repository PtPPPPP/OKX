from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import app.bootstrap as bootstrap
import app.cli as cli
from app.exchange.exceptions import ExchangeError, NetworkError, OrderRejected
from app.runtime.clock import BacktestClock
from app.services.demo_order_preflight import DemoOrderProposal, ProposalStatus
from app.services.demo_order_revalidation import ProposalRevalidationResult
from app.services.demo_proposal_application import DemoProposalApplication
from app.services.reconciliation import ReconciliationResult, ReconciliationStatus
from app.storage.repositories import TradingRepository
from tests.test_demo_write_boundary import FakeWriteClient
from tests.test_repository_transactions import make_prepared_repository


class _Client(FakeWriteClient):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.clock = BacktestClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.failure: Exception | None = None

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        self.events.append("write")
        if self.failure is not None:
            raise self.failure
        return super().place_order(*args, **kwargs)

    def close(self) -> None:
        self.events.append("client.close")


@pytest.fixture
def workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TradingRepository, DemoOrderProposal, _Client, list[str], dict[str, Any]]:
    repository, proposal = make_prepared_repository(tmp_path)
    events: list[str] = []
    client = _Client(events)
    switches: dict[str, Any] = {}

    class Session:
        def __init__(self, *args: Any) -> None:
            events.append("session.create")
            if switches.get("constructor"):
                raise ValueError("session construction failed")

        def start(self) -> None:
            events.append("session.start")
            if switches.get("start"):
                raise ValueError("session startup failed")

        @property
        def order_submission_ready(self) -> bool:
            return True

        def reconcile_result(self) -> ReconciliationResult:
            events.append("reconcile")
            return ReconciliationResult(
                ReconciliationStatus.BLOCKED
                if switches.get("reconcile")
                else ReconciliationStatus.HEALTHY,
                "fake reconciliation",
                0,
                0,
            )

        def close(self) -> None:
            events.append("session.close")
            if switches.get("close"):
                raise RuntimeError("session cleanup failed")

    class Revalidator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def revalidate(self, proposal_id: str) -> ProposalRevalidationResult:
            events.append("revalidate")
            current = repository.load_demo_order_proposal(proposal_id)
            assert current is not None
            passed = (
                not switches.get("revalidate")
                and current.status is ProposalStatus.READY_FOR_CONFIRMATION
            )
            if passed:
                repository.fence_demo_order_proposal(proposal_id)
            return ProposalRevalidationResult(proposal_id, passed, current.status, "fake check")

    monkeypatch.setattr(bootstrap, "DemoTradingSession", Session)
    monkeypatch.setattr(
        "app.services.demo_proposal_application.DemoOrderProposalRevalidator", Revalidator
    )
    monkeypatch.setattr(cli, "_demo", lambda *_: (None, None, repository, client))
    return repository, proposal, client, events, switches


def _submit(proposal: DemoOrderProposal) -> Any:
    return CliRunner().invoke(
        cli.app,
        ["submit-demo-order", "--proposal-id", proposal.proposal_id, "--confirm-demo-order"],
    )


def test_submit_cli_output_and_one_use_write_boundary(workflow: Any) -> None:
    repository, proposal, client, events, _ = workflow
    result = _submit(proposal)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "proposal_id": proposal.proposal_id,
        "client_order_id": proposal.client_order_id,
        "exchange_order_id": None,
        "state": "accepted",
        "filled_quantity": "0",
        "average_price": None,
        "submission_performed": True,
        "environment": "demo",
    }
    assert events == [
        "session.create",
        "session.start",
        "revalidate",
        "write",
        "reconcile",
        "session.close",
        "client.close",
    ]
    assert client.place_calls == 1
    repeated = _submit(proposal)
    assert repeated.exit_code == 1
    assert "proposal revalidation failed: fake check" in repeated.output
    assert client.place_calls == 1
    assert repository.count_controlled_demo_submissions() == 1


@pytest.mark.parametrize("passed", [True, False])
def test_revalidate_cli_json_and_exit_code(workflow: Any, passed: bool) -> None:
    _, proposal, client, events, switches = workflow
    switches["revalidate"] = not passed
    result = CliRunner().invoke(
        cli.app, ["revalidate-demo-order-proposal", "--proposal-id", proposal.proposal_id]
    )
    assert result.exit_code == (0 if passed else 1)
    assert json.loads(result.stdout) == {
        "proposal_id": proposal.proposal_id,
        "passed": passed,
        "status": "ready_for_confirmation",
        "reason": "fake check",
    }
    assert events[-2:] == ["session.close", "client.close"]
    assert client.place_calls == 0


@pytest.mark.parametrize("stage", ["constructor", "start", "revalidate", "close", "reconcile"])
def test_cli_exception_paths_release_resources(workflow: Any, stage: str) -> None:
    _, proposal, _, events, switches = workflow
    switches[stage] = True
    result = _submit(proposal)
    assert result.exit_code == 1
    assert events.count("client.close") == 1
    assert events.count("session.close") == (0 if stage == "constructor" else 1)
    if stage in {"constructor", "start", "revalidate"}:
        assert "write" not in events
    if stage == "reconcile":
        assert "订单提交后对账未通过" in result.output


@pytest.mark.parametrize(
    ("error", "status", "order_state"),
    [
        (OrderRejected("rejected"), ProposalStatus.CONSUMED, "rejected"),
        (NetworkError("network unknown"), ProposalStatus.UNKNOWN, "unknown"),
        (ExchangeError("HTTP 503"), ProposalStatus.UNKNOWN, "unknown"),
        (ExchangeError("ambiguous exchange error"), ProposalStatus.UNKNOWN, "unknown"),
    ],
)
def test_submission_error_outcomes_remain_recoverable(
    workflow: Any, error: Exception, status: ProposalStatus, order_state: str
) -> None:
    repository, proposal, client, events, _ = workflow
    client.failure = error
    result = _submit(proposal)
    assert result.exit_code == 1
    stored = repository.load_demo_order_proposal(proposal.proposal_id)
    assert stored.status is status
    assert stored.submission_performed
    assert repository.load_order(proposal.client_order_id).state.value == order_state
    assert events[-2:] == ["session.close", "client.close"]
    assert events.count("write") == 1
    assert repository.count_controlled_demo_submissions() == 1


def test_missing_proposal_cli_error_remains_compatible(workflow: Any) -> None:
    _, _, client, events, _ = workflow
    result = CliRunner().invoke(
        cli.app, ["submit-demo-order", "--proposal-id", "missing", "--confirm-demo-order"]
    )
    assert result.exit_code == 1
    assert "错误: proposal not found" in result.output
    assert client.place_calls == 0
    assert events[-2:] == ["session.close", "client.close"]


def test_confirmation_checked_before_any_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any) -> Any:
        pytest.fail("resource creation before explicit confirmation")

    monkeypatch.setattr(cli, "_demo", forbidden)
    result = CliRunner().invoke(cli.app, ["submit-demo-order", "--proposal-id", "test"])
    assert result.exit_code == 1
    assert "must explicitly provide --confirm-demo-order" in result.output


def test_keyboard_interrupt_still_closes_both_resources(workflow: Any) -> None:
    repository, _, client, events, _ = workflow
    with (
        pytest.raises(KeyboardInterrupt),
        bootstrap.open_demo_proposal_application(None, None, repository, client),
    ):
        raise KeyboardInterrupt
    assert events[-2:] == ["session.close", "client.close"]


def test_application_has_no_resource_ownership_or_cli_dependency() -> None:
    import inspect

    source = inspect.getsource(DemoProposalApplication)
    assert "typer" not in source
    assert ".close(" not in source
