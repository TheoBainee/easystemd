"""Tests for the doctor diagnostics."""

from __future__ import annotations

import pytest

from easystemd import config as cfg
from easystemd import doctor
from easystemd import systemd
from easystemd import templates
from easystemd.models import AppConfig


def _add_app(name="app1", binary="/usr/local/bin/x", **kw):
    base = dict(name=name, binary=binary, serve_args="web", upgrade_args="upgrade")
    base.update(kw)
    cfg.add_app(AppConfig(**base))


def _findings_by_level(report, level):
    return [f for f in report.findings if f.level == level]


def test_doctor_clean(tmp_home, fake_systemd, make_fake_binary):
    b = make_fake_binary("mybin")
    _add_app("a", b)
    # write coherent units so coherence check passes
    app = cfg.get_app("a")
    exe = templates.resolve_easystemd_exe()
    for unit, body in templates.render_all(app, exe).items():
        systemd.write_unit(unit, body)
    fake_systemd["state"]["enabled"].add(app.timer_unit)

    report = doctor.run_doctor(easystemd_exe=exe)

    assert report.ok, [str(f) for f in report.findings]
    assert not _findings_by_level(report, "error")


def test_doctor_detects_linger_disabled(tmp_home, fake_systemd, make_fake_binary):
    fake_systemd["state"]["linger"] = False
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    errs = _findings_by_level(report, "error")
    assert any(f.check == "linger" for f in errs)
    assert any("enable-linger" in (f.fix_hint or "") for f in errs)


def test_doctor_detects_missing_binary(tmp_home, fake_systemd, make_fake_binary):
    _add_app("a", "/no/such/binary")
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    errs = _findings_by_level(report, "error")
    assert any(f.check == "binary" and "not found" in f.message for f in errs)


def test_doctor_detects_non_executable_binary(tmp_home, fake_systemd, tmp_path):
    p = tmp_path / "noexec"
    p.write_text("data")
    p.chmod(0o644)  # not executable
    _add_app("a", str(p))
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    errs = _findings_by_level(report, "error")
    assert any(f.check == "binary" and "not executable" in f.message for f in errs)


def test_doctor_detects_missing_units(tmp_home, fake_systemd, make_fake_binary):
    b = make_fake_binary("mybin")
    _add_app("a", b)
    # do not write units
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    errs = _findings_by_level(report, "error")
    assert any(f.check == "units" and "missing" in f.message for f in errs)


def test_doctor_detects_stale_units(tmp_home, fake_systemd, make_fake_binary):
    b = make_fake_binary("mybin")
    _add_app("a", b)
    app = cfg.get_app("a")
    exe = "/x/easystemd"
    # write a STALE serve unit (wrong args)
    stale = templates.render_serve(app, exe).replace("ExecStart=" + b + " web", "ExecStart=" + b + " OLD")
    systemd.write_unit(app.serve_unit, stale)
    for unit, body in templates.render_all(app, exe).items():
        if unit != app.serve_unit:
            systemd.write_unit(unit, body)
    fake_systemd["state"]["enabled"].add(app.timer_unit)

    report = doctor.run_doctor(easystemd_exe=exe)
    warns = _findings_by_level(report, "warn")
    assert any(f.check == "units" and "differs" in f.message for f in warns)


def test_doctor_detects_disabled_timer(tmp_home, fake_systemd, make_fake_binary):
    b = make_fake_binary("mybin")
    _add_app("a", b)
    app = cfg.get_app("a")
    exe = "/x/easystemd"
    for unit, body in templates.render_all(app, exe).items():
        systemd.write_unit(unit, body)
    # timer NOT enabled
    report = doctor.run_doctor(easystemd_exe=exe)
    warns = _findings_by_level(report, "warn")
    assert any(f.check == "timer" and "not enabled" in f.message for f in warns)


def test_doctor_detects_verify_failure(tmp_home, fake_systemd, make_fake_binary):
    b = make_fake_binary("mybin")
    _add_app("a", b)
    app = cfg.get_app("a")
    exe = "/x/easystemd"
    for unit, body in templates.render_all(app, exe).items():
        systemd.write_unit(unit, body)
    fake_systemd["state"]["enabled"].add(app.timer_unit)
    fake_systemd["state"]["verify_ok"] = False
    fake_systemd["state"]["verify_stderr"] = "syntax error"

    report = doctor.run_doctor(easystemd_exe=exe)
    errs = _findings_by_level(report, "error")
    assert any(f.check == "verify" for f in errs)


def test_doctor_no_apps_message(tmp_home, fake_systemd):
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    ok = _findings_by_level(report, "ok")
    assert any(f.check == "apps" and "no apps" in f.message for f in ok)


def test_doctor_bus_unreachable(tmp_home, fake_systemd):
    fake_systemd["state"]["bus_ok"] = False
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    errs = _findings_by_level(report, "error")
    assert any(f.check == "user-bus" for f in errs)


def test_fix_linger_interactive_confirms(tmp_home, fake_systemd):
    fake_systemd["state"]["linger"] = False
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    # confirm=True
    ok = doctor.fix_linger_interactive(report, confirm=lambda prompt: True)
    assert ok is True
    assert fake_systemd["state"]["linger"] is True


def test_fix_linger_interactive_declined(tmp_home, fake_systemd):
    fake_systemd["state"]["linger"] = False
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    ok = doctor.fix_linger_interactive(report, confirm=lambda prompt: False)
    assert ok is False
    assert fake_systemd["state"]["linger"] is False


def test_fix_linger_noop_when_no_linger_error(tmp_home, fake_systemd):
    fake_systemd["state"]["linger"] = True
    report = doctor.run_doctor(easystemd_exe="/x/easystemd")
    ok = doctor.fix_linger_interactive(report, confirm=lambda prompt: True)
    assert ok is False
