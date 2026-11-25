from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from app.domain.events import EventBus
from app.domain.market import Instrument


class SessionRunner(Protocol):
    run_id: str

    def run(self) -> Any: ...


class SessionRepository(Protocol):
    def save_audit_record(
        self,
        *,
        record_type: str,
        run_id: str,
        mode: str,
        strategy_name: str,
        instrument_id: str,
        bar: str,
        payload: dict[str, Any],
    ) -> None: ...

    def save_audit_records(self, records: list[dict[str, Any]]) -> None: ...


class Closable(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    mode: str
    strategy_name: str
    instrument_id: str
    bar: str
    config_snapshot: dict[str, Any]


@dataclass(slots=True)
class TradingSession:
    """Lifecycle shell shared by backtest, demo one-shot and observe runners."""

    descriptor: SessionDescriptor
    instrument: Instrument
    runner: SessionRunner
    repository: SessionRepository
    event_bus: EventBus
    resources: tuple[Closable, ...] = ()
    result_handler: Callable[[Any], None] | None = None
    before_run: Callable[[], None] | None = None
    after_run: Callable[[], None] | None = None
    failure_handler: Callable[[BaseException], None] | None = None

    def run(self) -> Any:
        dimensions = {
            "run_id": self.runner.run_id,
            "mode": self.descriptor.mode,
            "strategy_name": self.descriptor.strategy_name,
            "instrument_id": self.descriptor.instrument_id,
            "bar": self.descriptor.bar,
        }
        self._record("run_started", self.descriptor.config_snapshot, dimensions)
        self._record("instrument_snapshot", asdict(self.instrument), dimensions)
        failure: BaseException | None = None
        try:
            if self.before_run is not None:
                self.before_run()
            result = self.runner.run()
            if self.result_handler is not None:
                self.result_handler(result)
            self.repository.save_audit_records(
                [
                    {
                        "record_type": "domain_event",
                        "payload": asdict(event),
                        **dimensions,
                    }
                    for event in self.event_bus.events
                ]
            )
            self._record("run_completed", {"status": "completed"}, dimensions)
            return result
        except BaseException as exc:
            failure = exc
            if self.failure_handler is not None:
                self.failure_handler(exc)
            self._record(
                "system_exception",
                {"exception_type": type(exc).__name__, "message": str(exc)},
                dimensions,
            )
            raise
        finally:
            close_error: Exception | None = None
            if self.after_run is not None:
                try:
                    self.after_run()
                except Exception as exc:
                    close_error = exc
            for resource in reversed(self.resources):
                try:
                    resource.close()
                except Exception as exc:
                    close_error = close_error or exc
            if failure is None and close_error is not None:
                raise close_error

    def run_backtest(self) -> Any:
        return self.run()

    def _record(
        self, record_type: str, payload: dict[str, Any], dimensions: dict[str, str]
    ) -> None:
        self.repository.save_audit_record(
            record_type=record_type,
            payload=payload,
            **dimensions,
        )
