"""Pydantic schemas for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.config import ACCEL_GROUPS


class AccelAddressUpdate(BaseModel):
    """Set the acceleration address (加速地址) of one or more groups."""

    group: str = Field(..., description="Acceleration group key")
    ip: str = Field("", description="IP address to accelerate to")

    @field_validator("group")
    @classmethod
    def _known_group(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ACCEL_GROUPS:
            raise ValueError(f"Unknown group. Valid groups: {', '.join(ACCEL_GROUPS)}")
        return value


class HostMappingCreate(BaseModel):
    """Create a manual hostname -> IP mapping."""

    host_name: str = Field(..., min_length=1, max_length=253)
    ip: str = Field(..., min_length=1)
    note: str = Field("", max_length=200)
    is_enabled: bool = True


class HostMappingUpdate(BaseModel):
    """Update an existing manual mapping."""

    host_name: str | None = Field(None, min_length=1, max_length=253)
    ip: str | None = Field(None, min_length=1)
    note: str | None = Field(None, max_length=200)
    is_enabled: bool | None = None


class SpeedTestRequest(BaseModel):
    """Run a speed test against a set of candidate addresses."""

    group: str | None = Field(None, description="Acceleration group to test")
    ips: list[str] = Field(default_factory=list, description="Explicit IP list")
    target: str | None = Field(None, description="Test URL override")
    limit: int = Field(20, ge=1, le=200, description="Max addresses to test")
    concurrency: int | None = Field(None, ge=1, le=32)
    with_ttl: bool = True


class SpeedTestResponse(BaseModel):
    """Result of a speed test run."""

    group: str
    target: str
    results: list[dict[str, Any]]
    best: dict[str, Any] | None = None
    elapsed_s: float = 0.0


class SettingsUpdate(BaseModel):
    """Partial settings update."""

    model_config = {"extra": "allow"}


class ResolveRequest(BaseModel):
    """Resolve a hostname through the acceleration rules."""

    hostname: str = Field(..., min_length=1, max_length=253)
    qtype: str = Field("A", description="A or AAAA")


class ResolveResponse(BaseModel):
    """Resolution outcome."""

    hostname: str
    ips: list[str]
    source: str
    rule_key: str | None = None
    group: str | None = None
    error: str | None = None
