"""Pydantic schema for reproducible Step 5 dataset generation plans."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator


DEVICE_IDS = ("call", "game", "tv1", "tv2", "tv3", "bulk", "laptop")
TRAFFIC_LABELS = ("interactive", "gaming", "streaming", "bulk")
RESERVED_STATIC_PORTS = frozenset({5201, 6001, 6002, 8080})
RATE_RE = re.compile(r"^[1-9][0-9]*(bit|kbit|mbit|gbit)$")


class PortRange(BaseModel):
    min_port: int = Field(default=30000, ge=1024, le=65535)
    max_port: int = Field(default=59999, ge=1024, le=65535)

    @model_validator(mode="after")
    def check_bounds(self) -> "PortRange":
        if self.min_port >= self.max_port:
            raise ValueError("min_port must be less than max_port")
        return self


class UdpDatasetSpec(BaseModel):
    id: str = Field(min_length=1)
    device_id: Literal["call", "game", "tv1", "tv2", "tv3", "bulk", "laptop"]
    label: Literal["interactive", "gaming"]
    kind: Literal["udp_dataset"]
    rate: float = Field(gt=0)
    duration_s: float = Field(gt=0)
    sessions: int = Field(default=1, ge=1)
    rounds: int = Field(default=1, ge=1)
    drain_ms: float = Field(default=150.0, ge=0)
    pattern: Literal["regular", "bursty"] = "regular"
    burst_size: int = Field(default=5, ge=1)
    intra_burst_gap_ms: float = Field(default=2.0, ge=0)
    min_payload_bytes: int = Field(default=160, ge=16, le=1472)
    max_payload_bytes: int = Field(default=160, ge=16, le=1472)
    start_delay_s: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def check_payload_and_burst(self) -> "UdpDatasetSpec":
        if self.min_payload_bytes > self.max_payload_bytes:
            raise ValueError("min_payload_bytes cannot exceed max_payload_bytes")
        if self.pattern == "bursty":
            burst_period = self.burst_size / self.rate
            intra_gap = self.intra_burst_gap_ms / 1000.0
            if (self.burst_size - 1) * intra_gap >= burst_period:
                raise ValueError(
                    f"Stream {self.id}: burst_size and intra_burst_gap_ms exceed burst period"
                )
        return self


class HttpChunksSpec(BaseModel):
    id: str = Field(min_length=1)
    device_id: Literal["call", "game", "tv1", "tv2", "tv3", "bulk", "laptop"]
    label: Literal["streaming"]
    kind: Literal["http_chunks"]
    duration_s: float = Field(gt=0)
    min_chunk_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    max_chunk_bytes: int = Field(ge=1, le=16 * 1024 * 1024)
    min_interval_s: float = Field(gt=0)
    max_interval_s: float = Field(gt=0)
    start_delay_s: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def check_ranges(self) -> "HttpChunksSpec":
        if self.min_chunk_bytes > self.max_chunk_bytes:
            raise ValueError("min_chunk_bytes cannot exceed max_chunk_bytes")
        if self.min_interval_s > self.max_interval_s:
            raise ValueError("min_interval_s cannot exceed max_interval_s")
        return self


class Iperf3BulkSpec(BaseModel):
    id: str = Field(min_length=1)
    device_id: Literal["call", "game", "tv1", "tv2", "tv3", "bulk", "laptop"]
    label: Literal["bulk"]
    kind: Literal["iperf3_tcp"]
    direction: Literal["upload", "download"]
    streams: int = Field(default=4, ge=1, le=32)
    rounds: int = Field(default=1, ge=1)
    round_duration_s: int = Field(default=1, ge=1)
    bitrate: str | None = None
    gap_s: float = Field(default=0.05, ge=0)
    start_delay_s: float = Field(default=0.0, ge=0)
    min_packets: int = Field(default=40, ge=1)
    min_bytes: int = Field(default=32768, ge=1)


TrafficSpec = Annotated[
    Union[UdpDatasetSpec, HttpChunksSpec, Iperf3BulkSpec],
    Field(discriminator="kind"),
]


class GeneratorConfig(BaseModel):
    config_id: str = Field(min_length=1)
    seed: int = Field(ge=1)
    description: str = ""
    modem_rate: str = "100mbit"
    capture_duration_s: float = Field(gt=0)
    streams: list[TrafficSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def check_streams_and_rate(self) -> "GeneratorConfig":
        if not RATE_RE.match(self.modem_rate):
            raise ValueError(f"Invalid modem_rate: {self.modem_rate}")

        seen_ids: set[str] = set()
        for stream in self.streams:
            if stream.id in seen_ids:
                raise ValueError(f"Duplicate stream id in {self.config_id}: {stream.id}")
            seen_ids.add(stream.id)
        return self


class DatasetPlan(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1)
    description: str = ""
    idle_timeout_s: float = Field(default=5.0, gt=0)
    port_range: PortRange = Field(default_factory=PortRange)
    configs: list[GeneratorConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def check_uniqueness(self) -> "DatasetPlan":
        config_ids: set[str] = set()
        seeds: set[int] = set()
        for cfg in self.configs:
            if cfg.config_id in config_ids:
                raise ValueError(f"Duplicate config_id: {cfg.config_id}")
            if cfg.seed in seeds:
                raise ValueError(f"Duplicate seed across configs: {cfg.seed}")
            config_ids.add(cfg.config_id)
            seeds.add(cfg.seed)
        return self
