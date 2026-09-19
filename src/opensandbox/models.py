"""Public API contracts; infrastructure placement belongs to Kubernetes."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opensandbox.utils.clock import utcnow
from opensandbox.utils.sizes import parse_bytes, parse_duration


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Network(Model):
    internet: bool = True
    private_network: Literal[False] = Field(default=False, alias="privateNetwork")


class CreateSandbox(Model):
    template: str | None = None
    image: str = Field(default="python:3.13-slim", min_length=1, max_length=512)
    cpu: float = Field(default=1, ge=0.01, le=1024, allow_inf_nan=False)
    memory: str | int = "1gb"
    disk: str | int = "10gb"
    timeout: str | int = 3600
    create_timeout: float = Field(default=120, ge=1, le=1800, allow_inf_nan=False)
    env: dict[str, str] = Field(default_factory=dict)
    workdir: str = "/workspace"
    network: Network = Field(default_factory=Network)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("memory", "disk")
    @classmethod
    def positive_size(cls, value):
        if parse_bytes(value) < 1024 * 1024:
            raise ValueError("size must be at least 1 MiB")
        return value

    @field_validator("timeout")
    @classmethod
    def lifetime(cls, value):
        seconds = parse_duration(value)
        if not math.isfinite(seconds) or not 1 <= seconds <= 604800:
            raise ValueError("timeout must be between 1 second and 7 days")
        return value

    @field_validator("workdir")
    @classmethod
    def absolute_path(cls, value):
        if not value.startswith("/") or "\0" in value:
            raise ValueError("workdir must be an absolute path without NUL")
        return value

    @field_validator("env")
    @classmethod
    def environment(cls, value):
        for key, item in value.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in item:
                raise ValueError("invalid environment variable")
            if key.startswith("OPENSANDBOX_"):
                raise ValueError("OPENSANDBOX_ environment variables are reserved")
        return value


class SandboxInfo(Model):
    id: str
    state: Literal["pending", "running", "destroyed", "expired", "failed", "lost"] = "pending"
    image: str
    cpu: float
    memory: int
    disk: int
    node: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    error: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    owner: str = "system"
    template: str = "base"
    internet: bool = True
    public_traffic: bool = True
    cleanup_pending: bool = True


class CommandRequest(Model):
    command: str | list[str]
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    timeout: float | None = Field(default=None, gt=0, le=604800, allow_inf_nan=False)
    background: bool = False

    @field_validator("command")
    @classmethod
    def not_empty(cls, value):
        if not value or (isinstance(value, list) and any(not arg or "\0" in arg for arg in value)):
            raise ValueError("command must not be empty or contain NUL")
        if isinstance(value, str) and "\0" in value:
            raise ValueError("command must not contain NUL")
        return value


class CommandInfo(Model):
    id: str
    status: Literal["running", "exited", "killed", "timed_out", "failed"]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


class TimeoutRequest(Model):
    timeout: str | int

    @field_validator("timeout")
    @classmethod
    def lifetime(cls, value):
        return CreateSandbox.lifetime(value)
