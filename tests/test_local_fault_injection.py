from __future__ import annotations

import json

import pytest

from app.exchange.exceptions import ExchangeError, NetworkError, RateLimitError, RequestTimeout
from app.testing.fault_injection import (
    FaultAction,
    FaultInjector,
    FaultPlan,
    FaultStep,
    VirtualClock,
)


class LocalAdapter:
    is_local_adapter = True


class ExternalAdapter:
    is_local_adapter = False


def test_fault_plan_is_serializable_deterministic_and_replayable() -> None:
    plan = FaultPlan(
        "timeout-then-recover",
        42,
        (
            FaultStep("private_rest.request.before_send", FaultAction.TIMEOUT),
            FaultStep("private_rest.request.before_send", FaultAction.PASS, occurrence=2),
        ),
    )
    traces = []
    for _ in range(2):
        injector = FaultInjector(plan, LocalAdapter(), VirtualClock())
        with pytest.raises(RequestTimeout):
            injector.inject("private_rest.request.before_send")
        injector.inject("private_rest.request.before_send")
        injector.assert_consumed()
        traces.append(injector.trace)
    assert json.dumps(plan.to_dict(), default=str)
    assert traces[0].entries == traces[1].entries
    assert traces[0].state_hash() == traces[1].state_hash()


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (FaultAction.RATE_LIMIT, RateLimitError),
        (FaultAction.SERVER_ERROR, ExchangeError),
        (FaultAction.CONNECTION_RESET, NetworkError),
        (FaultAction.MALFORMED_JSON, ValueError),
        (FaultAction.SCHEMA_INVALID, ValueError),
        (FaultAction.SEMANTIC_INVALID, ValueError),
    ],
)
def test_local_injector_classifies_rest_faults_without_network(
    action: FaultAction, error: type[Exception]
) -> None:
    injector = FaultInjector(
        FaultPlan("rest-fault", 7, (FaultStep("private_rest.response.before_parse", action),)),
        LocalAdapter(),
        VirtualClock(),
    )
    with pytest.raises(error):
        injector.inject("private_rest.response.before_parse")
    assert injector.trace.entries[0].action is action


def test_injector_rejects_external_adapter_unknown_point_and_unconsumed_plan() -> None:
    plan = FaultPlan(
        "guard", 1, (FaultStep("private_ws.event.before_coordinator", FaultAction.PASS),)
    )
    with pytest.raises(ValueError, match="local adapter"):
        FaultInjector(plan, ExternalAdapter(), VirtualClock())
    injector = FaultInjector(plan, LocalAdapter(), VirtualClock())
    with pytest.raises(ValueError, match="unknown"):
        injector.inject("not-an-injection-point")
    with pytest.raises(AssertionError, match="not triggered"):
        injector.assert_consumed()
