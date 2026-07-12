"""Pydantic v2 models for easystemd configuration."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z0-9-]+$")


class ExecType(str, Enum):
    """Mapping to systemd Service Type= values."""

    simple = "simple"
    forking = "forking"
    exec = "exec"
    notify = "notify"


class AppConfig(BaseModel):
    """Validated configuration for a single managed app.

    The ``binary`` field must be an absolute path resolved at ``add`` time via
    ``shutil.which()`` then ``Path.resolve()``. Schedule validation against
    ``systemd-analyze calendar`` is performed outside the model (in
    :mod:`easystemd.systemd`) so the model stays pure/testable without systemd.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(..., description="Unique slug [a-z0-9-]+, basis for unit names.")
    binary: str = Field(..., description="Absolute path to the managed binary.")
    serve_args: str = Field(..., description='Subcommand/args for the serve mode, e.g. "web".')
    upgrade_args: str = Field(..., description='Subcommand/args for the upgrade, e.g. "upgrade".')
    exec_type: ExecType = Field(ExecType.simple, description="systemd Type= for the serve service.")
    schedule: str = Field("Sun 04:00:00", description="OnCalendar expression for the upgrade timer.")
    working_dir: str = Field(
        default_factory=lambda: str(Path.home()),
        description="WorkingDirectory= for the serve service.",
    )
    env_file: Optional[str] = Field(None, description="EnvironmentFile= for the serve service.")
    restart_sec: int = Field(5, ge=0, description="RestartSec= for the serve service.")
    stop_timeout: int = Field(30, ge=1, description="TimeoutStopSec= for the serve service.")
    randomized_delay: int = Field(300, ge=0, description="RandomizedDelaySec= for the timer.")
    persistent: bool = Field(True, description="Persistent= for the timer (catch-up).")
    pre_upgrade_hook: Optional[str] = Field(None, description="Shell command run before stopping serve.")
    post_upgrade_hook: Optional[str] = Field(None, description="Shell command run after restarting serve.")
    health_check: Optional[str] = Field(None, description="Shell command; exit 0 = healthy.")
    health_check_retries: int = Field(5, ge=1, description="Number of health check attempts.")
    health_check_interval_sec: int = Field(3, ge=1, description="Seconds between health check attempts.")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not v or not NAME_RE.match(v):
            raise ValueError(
                "name must match [a-z0-9-]+ (lowercase alphanumerics and hyphens only)"
            )
        if v.startswith("-") or v.endswith("-"):
            raise ValueError("name must not start or end with a hyphen")
        return v

    @field_validator("binary")
    @classmethod
    def _validate_binary(cls, v: str) -> str:
        p = Path(v)
        if not p.is_absolute():
            raise ValueError(f"binary must be an absolute path, got: {v!r}")
        return str(p)

    @field_validator("working_dir", "env_file")
    @classmethod
    def _validate_optional_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        p = Path(v)
        if not p.is_absolute():
            raise ValueError(f"path must be absolute, got: {v!r}")
        return str(p)

    @model_validator(mode="after")
    def _validate_model(self) -> "AppConfig":
        if not self.serve_args.strip():
            raise ValueError("serve_args must not be empty")
        if not self.upgrade_args.strip():
            raise ValueError("upgrade_args must not be empty")
        return self

    # ---- unit naming helpers -------------------------------------------------

    @property
    def serve_unit(self) -> str:
        return f"easystemd-{self.name}-serve.service"

    @property
    def upgrade_unit(self) -> str:
        return f"easystemd-{self.name}-upgrade.service"

    @property
    def timer_unit(self) -> str:
        return f"easystemd-{self.name}-upgrade.timer"

    @property
    def all_units(self) -> list[str]:
        return [self.serve_unit, self.upgrade_unit, self.timer_unit]


class ConfigFile(BaseModel):
    """Top-level config.yaml structure: a list of apps under the ``apps:`` key."""

    model_config = ConfigDict(extra="forbid")

    apps: list[AppConfig] = Field(default_factory=list)

    def find(self, name: str) -> Optional[AppConfig]:
        for app in self.apps:
            if app.name == name:
                return app
        return None

    def names(self) -> list[str]:
        return [a.name for a in self.apps]
