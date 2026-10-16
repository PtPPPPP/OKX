"""Validated V3 architecture parameters; values come from configuration, not duplicated defaults."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

Window = Annotated[StrictInt, Field(gt=0, le=10000)]
Ratio = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class V3Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def reject_booleans(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(isinstance(item, bool) for item in value.values()):
            raise ValueError("V3 numeric parameters must not be booleans")
        return value


class HTFParameters(V3Parameters):
    htf_fast: Window
    htf_slow: Window

    @model_validator(mode="after")
    def ordered_windows(self) -> HTFParameters:
        if self.htf_fast > self.htf_slow:
            raise ValueError("HTF requires fast <= slow")
        return self


class PullbackParameters(HTFParameters):
    one_hour_fast: Window
    one_hour_slow: Window

    @model_validator(mode="after")
    def ordered_hourly(self) -> PullbackParameters:
        if self.one_hour_fast > self.one_hour_slow:
            raise ValueError("hourly fast must not exceed slow")
        return self


class HTFBreakoutParameters(HTFParameters):
    breakout: Window


class VolumeBreakoutParameters(V3Parameters):
    breakout: Window
    volume_window: Window
    volume_multiple: Ratio


class VolumeMomentumParameters(HTFParameters):
    momentum: Window
    volume_window: Window
    volume_multiple: Ratio


class ReversalParameters(V3Parameters):
    mean_window: Window
    deviation: Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)]
    volume_window: Window
    volume_multiple: Ratio


V3_PARAMETER_MODELS: dict[str, type[V3Parameters]] = {
    "htf_pullback_recovery": PullbackParameters,
    "htf_breakout": HTFBreakoutParameters,
    "relative_volume_breakout": VolumeBreakoutParameters,
    "htf_volume_momentum": VolumeMomentumParameters,
    "volume_exhaustion_reversal": ReversalParameters,
}


def validated_parameters(name: str, parameters: dict[str, Any]) -> V3Parameters:
    try:
        model = V3_PARAMETER_MODELS[name]
    except KeyError as exc:
        raise ValueError(f"unknown V3 architecture: {name}") from exc
    return model.model_validate(parameters)
