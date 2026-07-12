"""Wrappers around ``systemctl --user``, ``systemd-analyze`` and ``loginctl``.

Every subprocess invocation goes through :func:`run`, which uses
``subprocess.run(..., capture_output=True, text=True, check=False)`` and returns
a :class:`Result`. Callers inspect ``returncode`` explicitly — ``check=True`` is
never used so error context (stdout/stderr) is always preserved.

User-bus availability is detected lazily and surfaced as a clean
:class:`SystemdUnavailableError` rather than a raw stacktrace.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .config import user_units_dir


# ---- result + errors ------------------------------------------------------


@dataclass
class Result:
    """Structured result of a subprocess invocation."""

    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def __str__(self) -> str:
        return f"$ {' '.join(self.cmd)} -> rc={self.returncode}\nstdout: {self.stdout!r}\nstderr: {self.stderr!r}"


class SystemdError(Exception):
    """Raised when a systemctl operation fails with context."""


class SystemdUnavailableError(SystemdError):
    """Raised when the user systemd bus cannot be reached.

    Typically: no dbus session, container without systemd, broken
    ``DBUS_SESSION_BUS_ADDRESS`` / ``XDG_RUNTIME_DIR``.
    """

    pass


class LingerError(SystemdError):
    """Raised when linger interaction needs sudo / cannot proceed."""

    pass


# ---- core runner ----------------------------------------------------------


def run(cmd: Sequence[str], *, check: bool = False) -> Result:
    """Run ``cmd`` capturing output, never raising on non-zero by default.

    Honours ``check=False`` per project policy: callers inspect
    ``Result.returncode``. With ``check=True`` a failing command raises
    :class:`SystemdError` carrying the full :class:`Result`.
    """
    cmd_list = [str(c) for c in cmd]
    proc = subprocess.run(
        cmd_list,
        capture_output=True,
        text=True,
        check=False,
    )
    res = Result(cmd=cmd_list, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and not res.ok:
        raise SystemdError(f"command failed: {res}")
    return res


def _user(args: Sequence[str], *, check: bool = False) -> Result:
    return run(["systemctl", "--user", *args], check=check)


# ---- user bus availability ------------------------------------------------


def check_user_bus() -> None:
    """Raise :class:`SystemdUnavailableError` if the user bus is unreachable."""
    res = _user(["status"])
    # `systemctl --user status` returns non-zero (e.g. rc=1 with "Stopped") in
    # plenty of healthy situations, but a *connection* failure has a distinct
    # stderr. Distinguish connection errors from normal non-zero status.
    err = res.stderr.lower()
    conn_markers = (
        "failed to connect to bus" in err,
        "failed to connect to d-bus" in err,
        "unable to lookup bus" in err,
        ("no such file or directory" in err and "run" in err),
    )
    if any(conn_markers):
        raise SystemdUnavailableError(
            "could not reach the systemd --user bus. Common causes: no dbus session, "
            "container without systemd, or unset DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR.\n"
            f"stderr: {res.stderr.strip()}"
        )


# ---- unit lifecycle -------------------------------------------------------


def is_active(unit: str) -> bool:
    """True if ``unit`` is active. Non-active (incl. rc!=0) → False."""
    res = _user(["is-active", unit])
    return res.returncode == 0 and res.stdout.strip() == "active"


def is_enabled(unit: str) -> bool:
    """True if ``unit`` is enabled."""
    res = _user(["is-enabled", unit])
    return res.returncode == 0 and res.stdout.strip() == "enabled"


def start(unit: str) -> Result:
    return _user(["start", unit])


def stop(unit: str) -> Result:
    return _user(["stop", unit])


def enable(unit: str, *, now: bool = False) -> Result:
    args = ["enable"]
    if now:
        args.append("--now")
    args.append(unit)
    return _user(args)


def disable(unit: str, *, now: bool = False) -> Result:
    args = ["disable"]
    if now:
        args.append("--now")
    args.append(unit)
    return _user(args)


def reset_failed(unit: str) -> Result:
    return _user(["reset-failed", unit])


def daemon_reload() -> Result:
    return _user(["daemon-reload"])


def stop_and_wait(unit: str, timeout: int, *, poll_interval: float = 0.5) -> bool:
    """Stop ``unit`` and poll ``is-active`` until inactive or ``timeout`` reached.

    Returns ``True`` if the unit became inactive within ``timeout`` seconds,
    ``False`` otherwise (caller may still proceed — the upgrade must run).
    """
    stop(unit)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_active(unit):
            return True
        time.sleep(poll_interval)
    return not is_active(unit)


def unit_status(unit: str) -> Result:
    """Full ``systemctl --user status`` output for ``unit``."""
    return _user(["status", unit])


def show_unit(unit: str, *properties: str) -> dict[str, str]:
    """``systemctl --user show`` parsed into a dict. Empty if command failed."""
    args = ["show", unit]
    if properties:
        args.append("--")
        args.extend(properties)
    res = _user(args)
    out: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def next_elapse(timer_unit: str) -> Optional[str]:
    """Next trigger time for ``timer_unit`` (``NextElapseUSecRealtime``)."""
    props = show_unit(timer_unit, "NextElapseUSecRealtime")
    val = props.get("NextElapseUSecRealtime", "")
    return val or None


# ---- timers listing -------------------------------------------------------


def list_timers(pattern: str = "easystemd-*") -> Result:
    """``systemctl --user list-timers`` filtered to ``pattern``."""
    return _user(["list-timers", "--all", "--no-pager", pattern])


# ---- unit file I/O --------------------------------------------------------


def unit_path(unit: str) -> Path:
    """Where a given unit file lives under the user units dir."""
    return user_units_dir() / unit


def write_unit(unit: str, body: str) -> Path:
    """Atomically write ``body`` to the user unit file ``unit``."""
    from .config import atomic_write_text

    path = unit_path(unit)
    atomic_write_text(path, body, mode=0o644)
    return path


def remove_unit(unit: str) -> None:
    """Remove a unit file if it exists (no error if missing)."""
    path = unit_path(unit)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def unit_exists(unit: str) -> bool:
    return unit_path(unit).exists()


# ---- validation -----------------------------------------------------------


def verify_units(paths: Sequence[Path | str]) -> Result:
    """Run ``systemd-analyze verify`` on the given unit file paths.

    Returns the :class:`Result`; caller checks ``.ok``. Note: verify returns
    non-zero with stderr messages describing each problem.
    """
    return run(["systemd-analyze", "verify", *[str(p) for p in paths]])


def validate_calendar(expr: str) -> Result:
    """Validate an ``OnCalendar`` expression via ``systemd-analyze calendar``.

    Returns the :class:`Result``; ``.ok`` means valid.
    """
    return run(["systemd-analyze", "calendar", expr])


# ---- linger ---------------------------------------------------------------


def linger_enabled(user: Optional[str] = None) -> bool:
    """Check whether linger is enabled for ``user`` (default: current user).

    Uses ``loginctl show-user $USER -p Linger``. Returns ``True`` if enabled.
    """
    import os

    username = user or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not username:
        raise LingerError("could not determine current user name ($USER/$LOGNAME unset)")
    res = run(["loginctl", "show-user", username, "-p", "Linger"])
    if res.returncode != 0:
        # show-user may return non-zero if the user has no linger record yet;
        # treat absent record as linger=no.
        return False
    return "Linger=yes" in res.stdout.splitlines()


def linger_enable_command(user: Optional[str] = None) -> str:
    """Return the exact sudo command the user should run to enable linger."""
    import os

    username = user or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not username:
        raise LingerError("could not determine current user name ($USER/$LOGNAME unset)")
    return f"sudo loginctl enable-linger {username}"


def linger_enable(user: Optional[str] = None) -> Result:
    """Enable linger via sudo. Should only be called after explicit confirmation.

    Never invoked silently by the rest of the codebase.
    """
    import os

    username = user or os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not username:
        raise LingerError("could not determine current user name ($USER/$LOGNAME unset)")
    if not shutil.which("sudo"):
        raise LingerError("sudo is not available; run the command manually: " + linger_enable_command(username))
    return run(["sudo", "loginctl", "enable-linger", username])
