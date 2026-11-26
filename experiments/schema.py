from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PositiveFloat = Annotated[float, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
Port = Annotated[int, Field(ge=1, le=65535)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Configuration(StrictModel):
    name: Literal["no_qos", "static_qos", "adaptive"]
    enabled: bool
    reason: str | None = None

    @model_validator(mode="after")
    def validate_adaptive(self):
        if self.name == "adaptive":
            if self.enabled:
                raise ValueError(
                    "Adaptive cannot be enabled before Step 8"
                )
            if self.reason != "not_implemented":
                raise ValueError(
                    "Disabled adaptive configuration must specify "
                    "reason: not_implemented"
                )
        elif not self.enabled and self.reason is None:
            raise ValueError(
                "Disabled configurations must include a reason"
            )
        return self


class LinkEvent(StrictModel):
    at_s: NonNegativeFloat
    up_mbps: PositiveFloat
    down_mbps: PositiveFloat


class Link(StrictModel):
    up_mbps: PositiveFloat
    down_mbps: PositiveFloat
    delay_ms: NonNegativeFloat
    jitter_ms: NonNegativeFloat
    loss_pct: Annotated[float, Field(ge=0, le=100)]
    events: list[LinkEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_events(self):
        event_times = [event.at_s for event in self.events]
        if event_times != sorted(event_times):
            raise ValueError("Link events must be ordered by at_s")
        if len(event_times) != len(set(event_times)):
            raise ValueError("Link event times must be unique")
        return self


class TrafficFlow(StrictModel):
    id: str = Field(min_length=1)
    device: Literal[
        "call", "game", "tv1", "tv2", "tv3", "bulk", "laptop"
    ]
    kind: Literal["udp_echo", "http_chunks", "iperf3_tcp"]
    port: Port
    start_s: float
    stop_s: PositiveFloat

    pps: PositiveFloat | None = None
    payload_bytes: Annotated[int, Field(gt=0)] | None = None

    chunk_bytes: Annotated[int, Field(gt=0)] | None = None
    interval_s: PositiveFloat | None = None

    direction: Literal["upload", "download"] | None = None
    streams: Annotated[int, Field(ge=1, le=64)] | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self):
        if self.stop_s <= self.start_s:
            raise ValueError("stop_s must be greater than start_s")

        if self.kind == "udp_echo":
            if self.pps is None or self.payload_bytes is None:
                raise ValueError(
                    "udp_echo requires pps and payload_bytes"
                )
            if self.port not in (6001, 6002):
                raise ValueError(
                    "udp_echo must use port 6001 or 6002"
                )

        elif self.kind == "http_chunks":
            if self.chunk_bytes is None or self.interval_s is None:
                raise ValueError(
                    "http_chunks requires chunk_bytes and interval_s"
                )
            if self.port != 8080:
                raise ValueError("http_chunks must use port 8080")

        elif self.kind == "iperf3_tcp":
            if self.direction is None or self.streams is None:
                raise ValueError(
                    "iperf3_tcp requires direction and streams"
                )
            if self.port != 5201:
                raise ValueError("iperf3_tcp must use port 5201")

        return self


class Phase(StrictModel):
    name: Literal["baseline", "bulk_ramp", "bulk_steady"]
    from_s: NonNegativeFloat
    to_s: PositiveFloat

    @model_validator(mode="after")
    def validate_interval(self):
        if self.to_s <= self.from_s:
            raise ValueError("Phase to_s must exceed from_s")
        return self


class Validity(StrictModel):
    max_host_cpu_pct: Annotated[float, Field(gt=0, le=100)]
    min_samples_per_phase: Annotated[int, Field(gt=0)]
    expect_modem_rate_match: bool


class Scenario(StrictModel):
    schema_version: Literal[1]
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    seed: Annotated[int, Field(ge=0)]
    repetitions: Annotated[int, Field(ge=1)]
    warmup_s: NonNegativeFloat
    duration_s: PositiveFloat
    randomization: Literal["blocked"]

    configurations: list[Configuration]
    link: Link
    traffic: list[TrafficFlow]
    phases: list[Phase]
    validity: Validity

    @model_validator(mode="after")
    def validate_scenario(self):
        config_names = [config.name for config in self.configurations]
        if len(config_names) != len(set(config_names)):
            raise ValueError("Configuration names must be unique")

        enabled = [c.name for c in self.configurations if c.enabled]
        if set(enabled) != {"no_qos", "static_qos"}:
            raise ValueError(
                "Exactly no_qos and static_qos must be enabled"
            )

        if "adaptive" not in config_names:
            raise ValueError(
                "Adaptive placeholder must remain in the configuration list"
            )

        flow_ids = [flow.id for flow in self.traffic]
        if len(flow_ids) != len(set(flow_ids)):
            raise ValueError("Traffic flow IDs must be unique")

        devices = [flow.device for flow in self.traffic]
        if len(devices) != len(set(devices)):
            raise ValueError("Only one primary flow per device is supported")

        for flow in self.traffic:
            if flow.start_s < -self.warmup_s:
                raise ValueError(
                    f"Flow {flow.id} starts before warm-up begins"
                )
            if flow.stop_s > self.duration_s:
                raise ValueError(
                    f"Flow {flow.id} stops after measurement ends"
                )

        phase_names = [phase.name for phase in self.phases]
        if len(phase_names) != len(set(phase_names)):
            raise ValueError("Phase names must be unique")

        if not self.phases:
            raise ValueError("At least one phase is required")

        ordered_phases = sorted(
            self.phases, key=lambda phase: phase.from_s
        )

        if ordered_phases[0].from_s != 0:
            raise ValueError("Phases must start at t=0")

        if ordered_phases[-1].to_s != self.duration_s:
            raise ValueError(
                "Phases must end at the measurement duration"
            )

        for previous, current in zip(
            ordered_phases, ordered_phases[1:]
        ):
            if previous.to_s != current.from_s:
                raise ValueError(
                    "Phases must be contiguous without gaps or overlap"
                )

        for event in self.link.events:
            if event.at_s > self.duration_s:
                raise ValueError(
                    "Link events cannot occur after measurement ends"
                )

        return self
