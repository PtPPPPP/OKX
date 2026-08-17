from __future__ import annotations

from uuid import uuid4

from app.runtime.clock import Clock, SystemClock


def new_client_order_id(clock: Clock | None = None) -> str:
    timestamp = (clock or SystemClock()).now().strftime("%Y%m%d%H%M%S")
    return f"oq{timestamp}{uuid4().hex[:12]}"
