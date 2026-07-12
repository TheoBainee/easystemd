"""Tests for systemd wrappers and unit template rendering.

No real ``systemctl`` is invoked: ``fake_systemd`` stubs all side-effects, and
``validate_calendar``/``verify_units`` are exercised via the stubs. The real
``systemd-analyze calendar`` is only used in the marked integration test.
"""

from __future__ import annotations

import pytest

from easystemd import systemd
from easystemd import templates
from easystemd.models import AppConfig, ExecType


# ---- template rendering ---------------------------------------------------


def make_app(**kw) -> AppConfig:
    base = dict(name="mon-binaire", binary="/usr/local/bin/mon_binaire",
                serve_args="web", upgrade_args="upgrade")
    base.update(kw)
    return AppConfig(**base)


def test_render_serve_basic():
    app = make_app()
    out = templates.render_serve(app, "/home/u/.local/bin/easystemd")
    assert "Description=easystemd - mon-binaire (serve)" in out
    assert "Type=simple" in out
    assert "ExecStart=/usr/local/bin/mon_binaire web" in out
    assert "Restart=always" in out
    assert "RestartSec=5" in out
    assert "TimeoutStopSec=30" in out
    assert "WantedBy=default.target" in out


def test_render_serve_with_env_file():
    app = make_app(env_file="/etc/mon/env")
    out = templates.render_serve(app, "/home/u/.local/bin/easystemd")
    assert "EnvironmentFile=/etc/mon/env" in out


def test_render_serve_no_env_file_when_none():
    app = make_app()
    out = templates.render_serve(app, "/home/u/.local/bin/easystemd")
    assert "EnvironmentFile=" not in out


def test_render_serve_exec_type():
    app = make_app(exec_type=ExecType.notify)
    out = templates.render_serve(app, "/x/easystemd")
    assert "Type=notify" in out


def test_render_upgrade_uses_absolute_easystemd():
    app = make_app()
    out = templates.render_upgrade(app, "/home/u/.local/bin/easystemd")
    assert "Type=oneshot" in out
    assert "ExecStart=/home/u/.local/bin/easystemd _run-upgrade mon-binaire" in out
    assert "After=easystemd-mon-binaire-serve.service" in out


def test_render_timer():
    app = make_app(schedule="Mon 02:00", randomized_delay=120, persistent=False)
    out = templates.render_timer(app, "/x/easystemd")
    assert "OnCalendar=Mon 02:00" in out
    assert "RandomizedDelaySec=120" in out
    assert "Persistent=false" in out
    assert "WantedBy=timers.target" in out


def test_render_all_three_units():
    app = make_app()
    units = templates.render_all(app, "/x/easystemd")
    assert set(units.keys()) == {
        "easystemd-mon-binaire-serve.service",
        "easystemd-mon-binaire-upgrade.service",
        "easystemd-mon-binaire-upgrade.timer",
    }


def test_all_paths_in_units_are_absolute():
    """Definition-of-done: no relative paths in any generated unit."""
    app = make_app(env_file="/etc/mon/env")
    for body in templates.render_all(app, "/abs/easystemd").values():
        for line in body.splitlines():
            if line.startswith("ExecStart="):
                path = line.split("=", 1)[1].split()[0]
                assert path.startswith("/"), f"ExecStart path not absolute: {path}"
            if line.startswith("WorkingDirectory="):
                assert line.split("=", 1)[1].startswith("/")
            if line.startswith("EnvironmentFile="):
                assert line.split("=", 1)[1].startswith("/")


# ---- unit naming ----------------------------------------------------------


def test_unit_naming_convention():
    app = make_app(name="my-app")
    assert app.serve_unit == "easystemd-my-app-serve.service"
    assert app.upgrade_unit == "easystemd-my-app-upgrade.service"
    assert app.timer_unit == "easystemd-my-app-upgrade.timer"
    assert len(app.all_units) == 3


# ---- systemd wrappers via fake_systemd ------------------------------------


def test_validate_calendar_calls_stub(tmp_home, fake_systemd):
    systemd.validate_calendar("Sun 04:00")
    assert ("validate_calendar", "Sun 04:00") in fake_systemd["calls"]


def test_validate_calendar_invalid(tmp_home, fake_systemd):
    fake_systemd["state"]["calendar_ok"] = False
    res = systemd.validate_calendar("garbage")
    assert not res.ok


def test_verify_units_ok(tmp_home, fake_systemd):
    res = systemd.verify_units(["/tmp/x.service"])
    assert res.ok


def test_verify_units_failure(tmp_home, fake_systemd):
    fake_systemd["state"]["verify_ok"] = False
    fake_systemd["state"]["verify_stderr"] = "bad unit"
    res = systemd.verify_units(["/tmp/x.service"])
    assert not res.ok
    assert "bad unit" in res.stderr


def test_write_and_remove_unit(tmp_home, fake_systemd):
    path = systemd.write_unit("foo.service", "[Service]\nExecStart=/bin/true\n")
    assert path.exists()
    assert systemd.unit_exists("foo.service")
    systemd.remove_unit("foo.service")
    assert not systemd.unit_exists("foo.service")


def test_remove_unit_idempotent(tmp_home, fake_systemd):
    # removing a non-existing unit should not raise
    systemd.remove_unit("never-existed.service")


def test_linger_enabled_stub(tmp_home, fake_systemd):
    fake_systemd["state"]["linger"] = True
    assert systemd.linger_enabled() is True
    fake_systemd["state"]["linger"] = False
    assert systemd.linger_enabled() is False


def test_linger_enable_command_format(tmp_home, fake_systemd, monkeypatch):
    monkeypatch.setenv("USER", "alice")
    cmd = systemd.linger_enable_command()
    assert cmd == "sudo loginctl enable-linger alice"


def test_check_user_bus_ok(tmp_home, fake_systemd):
    # should not raise
    systemd.check_user_bus()


def test_check_user_bus_failure(tmp_home, fake_systemd):
    fake_systemd["state"]["bus_ok"] = False
    with pytest.raises(systemd.SystemdUnavailableError):
        systemd.check_user_bus()


def test_stop_and_wait(tmp_home, fake_systemd):
    fake_systemd["state"]["active"].add("u.service")
    ok = systemd.stop_and_wait("u.service", 5, poll_interval=0.01)
    assert ok is True
    assert "u.service" not in fake_systemd["state"]["active"]


def test_result_ok_property():
    r = systemd.Result(["x"], 0, "out", "")
    assert r.ok is True
    r2 = systemd.Result(["x"], 1, "", "err")
    assert r2.ok is False


def test_run_never_raises_on_nonzero(tmp_home):
    # use a real command that returns non-zero; run() must not raise
    r = systemd.run(["sh", "-c", "exit 7"])
    assert r.returncode == 7
    assert r.ok is False


# ---- resolve_easystemd_exe ------------------------------------------------


def test_resolve_easystemd_exe(tmp_home):
    exe = templates.resolve_easystemd_exe()
    assert exe.startswith("/")
    assert exe.endswith("easystemd")


# ---- integration marker ---------------------------------------------------


@pytest.mark.integration
def test_real_calendar_validation():
    """Uses the real systemd-analyze calendar (skipped by default)."""
    res = systemd.validate_calendar("Sun 04:00")
    assert res.ok
