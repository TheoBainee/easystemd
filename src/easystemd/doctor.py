"""Environment diagnostics: ``easystemd doctor``.

Performs read-only checks and, with ``--fix`` (and explicit confirmation for
anything requiring sudo), can repair simple drift. Nothing requiring sudo is
ever run silently — the user is always shown the exact command first.

Checks:
- linger enabled for the current user
- user systemd bus reachable
- for each app: binary present + executable; units present on disk and coherent
  with the current config (detects a config edited without regenerating units);
  timer active with a next scheduled run
- all on-disk units pass ``systemd-analyze verify``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config as cfg
from . import systemd
from . import templates


@dataclass
class Finding:
    level: str  # "ok" | "warn" | "error"
    scope: str  # "global" | app name
    check: str
    message: str
    fix_hint: Optional[str] = None  # command or instruction to remediate


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)


# ---- individual checks ----------------------------------------------------


def _check_linger(report: DoctorReport) -> None:
    try:
        if systemd.linger_enabled():
            report.add(Finding("ok", "global", "linger", "linger is enabled for the current user"))
        else:
            cmd = systemd.linger_enable_command()
            report.add(
                Finding(
                    "error",
                    "global",
                    "linger",
                    "linger is NOT enabled — user services won't survive logout / boot without it.",
                    fix_hint=cmd,
                )
            )
    except systemd.LingerError as e:
        report.add(Finding("error", "global", "linger", f"could not check linger: {e}"))


def _check_bus(report: DoctorReport) -> None:
    try:
        systemd.check_user_bus()
        report.add(Finding("ok", "global", "user-bus", "systemd --user bus is reachable"))
    except systemd.SystemdUnavailableError as e:
        report.add(Finding("error", "global", "user-bus", str(e)))


def _check_binary(report: DoctorReport, app) -> None:
    p = Path(app.binary)
    if not p.exists():
        report.add(
            Finding(
                "error",
                app.name,
                "binary",
                f"binary not found at {app.binary}",
                fix_hint=f"reinstall the binary, or `easystemd edit {app.name} --binary /new/path`",
            )
        )
        return
    if not os.access(p, os.X_OK):
        report.add(Finding("error", app.name, "binary", f"binary {app.binary} is not executable"))
        return
    report.add(Finding("ok", app.name, "binary", f"binary present and executable: {app.binary}"))


def _check_units_coherence(report: DoctorReport, app, easystemd_exe: str) -> None:
    """Compare on-disk units with a fresh render from current config."""
    try:
        rendered = templates.render_all(app, easystemd_exe)
    except Exception as e:
        report.add(Finding("error", app.name, "units", f"could not render units from config: {e}"))
        return
    for unit, body in rendered.items():
        path = systemd.unit_path(unit)
        if not path.exists():
            report.add(
                Finding(
                    "error",
                    app.name,
                    "units",
                    f"{unit} missing on disk (run `easystemd edit {app.name}` to regenerate)",
                    fix_hint=f"easystemd edit {app.name}",
                )
            )
            continue
        on_disk = path.read_text(encoding="utf-8")
        if on_disk != body:
            report.add(
                Finding(
                    "warn",
                    app.name,
                    "units",
                    f"{unit} on disk differs from current config (stale unit)",
                    fix_hint=f"easystemd edit {app.name}",
                )
            )


def _check_units_exist(report: DoctorReport, app) -> list[Path]:
    """Ensure all three unit files exist; return the list of existing paths."""
    paths = []
    for unit in app.all_units:
        path = systemd.unit_path(unit)
        if path.exists():
            paths.append(path)
        else:
            report.add(
                Finding(
                    "error",
                    app.name,
                    "units",
                    f"{unit} missing on disk",
                    fix_hint=f"easystemd edit {app.name}",
                )
            )
    return paths


def _check_timer_active(report: DoctorReport, app) -> None:
    if not systemd.unit_exists(app.timer_unit):
        return  # already reported by _check_units_exist
    if not systemd.is_enabled(app.timer_unit):
        report.add(
            Finding(
                "warn",
                app.name,
                "timer",
                f"{app.timer_unit} is not enabled — scheduled upgrades won't fire",
                fix_hint=f"systemctl --user enable {app.timer_unit}",
            )
        )
        return
    nxt = systemd.next_elapse(app.timer_unit)
    if nxt:
        report.add(Finding("ok", app.name, "timer", f"{app.timer_unit} enabled; next: {nxt}"))
    else:
        report.add(Finding("ok", app.name, "timer", f"{app.timer_unit} enabled"))


def _check_verify(report: DoctorReport, app, paths: list[Path]) -> None:
    if not paths:
        return
    res = systemd.verify_units(paths)
    if res.ok:
        report.add(Finding("ok", app.name, "verify", f"{len(paths)} unit(s) pass systemd-analyze verify"))
    else:
        report.add(
            Finding(
                "error",
                app.name,
                "verify",
                f"systemd-analyze verify failed:\n{res.stderr.strip() or res.stdout.strip()}",
                fix_hint=f"easystemd edit {app.name}",
            )
        )


# ---- orchestration --------------------------------------------------------


def run_doctor(*, easystemd_exe: Optional[str] = None) -> DoctorReport:
    """Run all read-only checks and return a :class:`DoctorReport`."""
    report = DoctorReport()
    _check_linger(report)
    _check_bus(report)

    try:
        cfg_file = cfg.load_config()
    except cfg.ConfigError as e:
        report.add(Finding("error", "global", "config", f"cannot load config: {e}"))
        return report

    exe = easystemd_exe
    if exe is None:
        try:
            exe = templates.resolve_easystemd_exe()
        except FileNotFoundError as e:
            report.add(Finding("error", "global", "easystemd-exe", str(e)))
            return report

    if not cfg_file.apps:
        report.add(Finding("ok", "global", "apps", "no apps configured yet"))

    for app in cfg_file.apps:
        _check_binary(report, app)
        _check_units_coherence(report, app, exe)
        paths = _check_units_exist(report, app)
        _check_timer_active(report, app)
        _check_verify(report, app, paths)

    return report


def fix_linger_interactive(report: DoctorReport, *, confirm) -> bool:
    """Offer to enable linger. ``confirm`` is a callable(prompt) -> bool.

    Returns True if linger was (successfully) enabled. Never runs sudo without
    an explicit confirmation.
    """
    linger_err = [f for f in report.errors if f.check == "linger"]
    if not linger_err:
        return False
    cmd = systemd.linger_enable_command()
    if not confirm(
        f"Enable linger by running: {cmd}\nProceed? [y/N] "
    ):
        return False
    res = systemd.linger_enable()
    return res.ok
