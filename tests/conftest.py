"""Shared pytest fixtures.

Key guarantees per ProjectDescription section 11:
- No test ever invokes a real ``systemctl``/``loginctl`` on the dev/CI machine.
- ``Path.home()`` / ``$HOME`` are overridden via ``tmp_home`` so the real user
  config is never touched.
- All ``subprocess.run`` calls are mockable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional

import pytest


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Isolate HOME + XDG dirs and PATH so tests never touch real config/units.

    Sets up:
    - ``$HOME`` → tmp_path/home
    - ``$XDG_CONFIG_HOME`` → tmp_path/home/.config
    - ``$XDG_STATE_HOME`` → tmp_path/home/.local/state
    - a fake ``easystemd`` executable on a controlled PATH so
      ``resolve_easystemd_exe`` works without the real install
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    xdg_config = home / ".config"
    xdg_state = home / ".local" / "state"
    xdg_config.mkdir(parents=True, exist_ok=True)
    xdg_state.mkdir(parents=True, exist_ok=True)
    units_dir = xdg_config / "systemd" / "user"
    units_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))

    # fake easystemd on PATH so resolve_easystemd_exe succeeds
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "easystemd"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    return home


@pytest.fixture
def fake_systemd(monkeypatch):
    """Replace all ``easystemd.systemd`` side-effecting functions with stubs.

    Records calls in ``calls`` and lets tests configure return values via the
    returned ``stubs`` dict. ``is_active``/``is_enabled`` default to False.
    """
    from easystemd import systemd

    calls: list[tuple] = []
    state: dict[str, Any] = {
        "active": set(),   # unit names currently active
        "enabled": set(),  # unit names currently enabled
        "unit_files": {},  # unit name -> body on disk (simulated)
        "linger": True,
        "bus_ok": True,
        "calendar_ok": True,
        "verify_ok": True,
        "verify_stderr": "",
        "next_elapse": "Sun 2026-07-19 04:02:43 CEST",
    }

    def _is_active(unit):
        calls.append(("is_active", unit))
        return unit in state["active"]

    def _is_enabled(unit):
        calls.append(("is_enabled", unit))
        return unit in state["enabled"]

    def _start(unit):
        calls.append(("start", unit))
        state["active"].add(unit)
        from easystemd.systemd import Result
        return Result(["systemctl", "--user", "start", unit], 0, "", "")

    def _stop(unit):
        calls.append(("stop", unit))
        state["active"].discard(unit)
        from easystemd.systemd import Result
        return Result(["systemctl", "--user", "stop", unit], 0, "", "")

    def _stop_and_wait(unit, timeout, **kw):
        calls.append(("stop_and_wait", unit, timeout))
        state["active"].discard(unit)
        return True

    def _enable(unit, *, now=False):
        calls.append(("enable", unit, now))
        state["enabled"].add(unit)
        if now:
            state["active"].add(unit)
        from easystemd.systemd import Result
        return Result(["systemctl", "--user", "enable", unit], 0, "", "")

    def _disable(unit, *, now=False):
        calls.append(("disable", unit, now))
        state["enabled"].discard(unit)
        if now:
            state["active"].discard(unit)
        from easystemd.systemd import Result
        return Result(["systemctl", "--user", "disable", unit], 0, "", "")

    def _reset_failed(unit):
        calls.append(("reset_failed", unit))
        from easystemd.systemd import Result
        return Result(["systemctl", "--user", "reset-failed", unit], 0, "", "")

    def _daemon_reload():
        calls.append(("daemon_reload",))
        from easystemd.systemd import Result
        return Result(["systemctl", "--user", "daemon-reload"], 0, "", "")

    def _unit_status(unit):
        calls.append(("unit_status", unit))
        from easystemd.systemd import Result
        return Result(["systemctl", "--user", "status", unit], 0, f"status of {unit}\n", "")

    def _show_unit(unit, *props):
        calls.append(("show_unit", unit, props))
        return {"NextElapseUSecRealtime": state["next_elapse"]} if "NextElapseUSecRealtime" in props else {}

    def _next_elapse(timer_unit):
        calls.append(("next_elapse", timer_unit))
        return state["next_elapse"]

    def _unit_path(unit):
        from easystemd.config import user_units_dir
        return user_units_dir() / unit

    def _write_unit(unit, body):
        path = _unit_path(unit)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        state["unit_files"][unit] = body
        calls.append(("write_unit", unit))
        return path

    def _remove_unit(unit):
        path = _unit_path(unit)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        state["unit_files"].pop(unit, None)
        calls.append(("remove_unit", unit))

    def _unit_exists(unit):
        return _unit_path(unit).exists()

    def _verify_units(paths):
        calls.append(("verify_units", [str(p) for p in paths]))
        from easystemd.systemd import Result
        return Result(["systemd-analyze", "verify", *[str(p) for p in paths]],
                      0 if state["verify_ok"] else 1, "", state["verify_stderr"])

    def _validate_calendar(expr):
        calls.append(("validate_calendar", expr))
        from easystemd.systemd import Result
        return Result(["systemd-analyze", "calendar", expr],
                      0 if state["calendar_ok"] else 1, "", "invalid calendar" if not state["calendar_ok"] else "")

    def _linger_enabled(user=None):
        calls.append(("linger_enabled", user))
        return state["linger"]

    def _linger_enable_command(user=None):
        calls.append(("linger_enable_command", user))
        u = user or os.environ.get("USER") or os.environ.get("LOGNAME") or "testuser"
        return f"sudo loginctl enable-linger {u}"

    def _linger_enable(user=None):
        calls.append(("linger_enable", user))
        state["linger"] = True
        u = user or os.environ.get("USER") or os.environ.get("LOGNAME") or "testuser"
        from easystemd.systemd import Result
        return Result(["sudo", "loginctl", "enable-linger", u], 0, "", "")

    def _check_user_bus():
        if not state["bus_ok"]:
            from easystemd.systemd import SystemdUnavailableError
            raise SystemdUnavailableError("simulated bus failure")

    monkeypatch.setattr(systemd, "is_active", _is_active)
    monkeypatch.setattr(systemd, "is_enabled", _is_enabled)
    monkeypatch.setattr(systemd, "start", _start)
    monkeypatch.setattr(systemd, "stop", _stop)
    monkeypatch.setattr(systemd, "stop_and_wait", _stop_and_wait)
    monkeypatch.setattr(systemd, "enable", _enable)
    monkeypatch.setattr(systemd, "disable", _disable)
    monkeypatch.setattr(systemd, "reset_failed", _reset_failed)
    monkeypatch.setattr(systemd, "daemon_reload", _daemon_reload)
    monkeypatch.setattr(systemd, "unit_status", _unit_status)
    monkeypatch.setattr(systemd, "show_unit", _show_unit)
    monkeypatch.setattr(systemd, "next_elapse", _next_elapse)
    monkeypatch.setattr(systemd, "write_unit", _write_unit)
    monkeypatch.setattr(systemd, "remove_unit", _remove_unit)
    monkeypatch.setattr(systemd, "unit_exists", _unit_exists)
    monkeypatch.setattr(systemd, "verify_units", _verify_units)
    monkeypatch.setattr(systemd, "validate_calendar", _validate_calendar)
    monkeypatch.setattr(systemd, "linger_enabled", _linger_enabled)
    monkeypatch.setattr(systemd, "linger_enable_command", _linger_enable_command)
    monkeypatch.setattr(systemd, "linger_enable", _linger_enable)
    monkeypatch.setattr(systemd, "check_user_bus", _check_user_bus)

    return {"calls": calls, "state": state}


@pytest.fixture
def make_fake_binary(tmp_path):
    """Factory creating an executable fake binary and returning its absolute path."""
    def _make(name: str = "fakebin", body: str = "#!/bin/sh\nexit 0\n") -> str:
        p = tmp_path / name
        p.write_text(body)
        p.chmod(0o755)
        return str(p)
    return _make
