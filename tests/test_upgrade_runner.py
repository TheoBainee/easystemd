"""Tests for the upgrade runner — the critical core.

Per section 11, the priority behaviour to verify: the serve service is always
restarted, even when the upgrade command raises an exception or returns a
non-zero exit code.
"""

from __future__ import annotations

import pytest

from easystemd import config as cfg
from easystemd import state
from easystemd import systemd
from easystemd import upgrade_runner as ur
from easystemd.models import AppConfig


def _add_app(tmp_home, **overrides) -> AppConfig:
    base = dict(
        name="app1",
        binary="/usr/local/bin/mon_binaire",
        serve_args="web",
        upgrade_args="upgrade",
    )
    base.update(overrides)
    app = AppConfig(**base)
    cfg.add_app(app)
    return app


def _calls(fake_systemd, kind):
    return [c for c in fake_systemd["calls"] if c[0] == kind]


def test_serve_restarted_when_upgrade_returns_nonzero(tmp_home, fake_systemd, monkeypatch):
    """THE critical test: non-zero upgrade exit code still restarts the serve."""
    _add_app(tmp_home)
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (1, "out", "err", 1.0))
    monkeypatch.setattr(ur, "_run_shell", lambda cmd, env=None, timeout=None: systemd.Result(["sh", "-c", cmd], 0, "", ""))

    rc = ur.run_upgrade("app1")

    starts = _calls(fake_systemd, "start")
    assert ("start", "easystemd-app1-serve.service") in [c[:2] for c in starts]
    assert rc == 1
    st = state.read_state("app1")
    assert st["status"] == "failed"
    assert st["upgrade_exit_code"] == 1
    assert st["serve_restarted"] is True
    assert st["success"] is False


def test_serve_restarted_when_upgrade_raises_exception(tmp_home, fake_systemd, monkeypatch):
    """THE critical test: a Python exception during upgrade still restarts the serve."""
    _add_app(tmp_home)
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ur, "_run_shell", lambda cmd, env=None, timeout=None: systemd.Result(["sh", "-c", cmd], 0, "", ""))

    rc = ur.run_upgrade("app1")

    starts = _calls(fake_systemd, "start")
    assert ("start", "easystemd-app1-serve.service") in [c[:2] for c in starts]
    assert rc == 1
    st = state.read_state("app1")
    assert st["status"] == "failed"
    assert st["upgrade_exit_code"] is None
    assert st["serve_restarted"] is True
    assert "boom" in st["stderr"]


def test_success_path_exit_zero(tmp_home, fake_systemd, monkeypatch):
    """Happy path: upgrade exits 0 → success, serve restarted."""
    _add_app(tmp_home)
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (0, "upgraded", "", 0.5))
    monkeypatch.setattr(ur, "_run_shell", lambda cmd, env=None, timeout=None: systemd.Result(["sh", "-c", cmd], 0, "", ""))

    rc = ur.run_upgrade("app1")

    assert rc == 0
    st = state.read_state("app1")
    assert st["status"] == "success"
    assert st["upgrade_exit_code"] == 0
    assert st["serve_restarted"] is True
    assert st["success"] is True


def test_stop_called_before_start(tmp_home, fake_systemd, monkeypatch):
    """Ordering: stop_and_wait happens, then start (in finally)."""
    _add_app(tmp_home)
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (0, "", "", 0.1))
    monkeypatch.setattr(ur, "_run_shell", lambda cmd, env=None, timeout=None: systemd.Result(["sh", "-c", cmd], 0, "", ""))

    ur.run_upgrade("app1")

    seq = [c[0] for c in fake_systemd["calls"] if c[0] in ("stop_and_wait", "start")]
    assert "stop_and_wait" in seq
    assert "start" in seq
    assert seq.index("stop_and_wait") < seq.index("start")


def test_pre_hook_failure_aborts_before_stop(tmp_home, fake_systemd, monkeypatch):
    """pre_upgrade_hook failure → abort, serve is NOT stopped NOR restarted."""
    _add_app(tmp_home, pre_upgrade_hook="echo pre")
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (0, "", "", 0.1))

    def fake_shell(cmd, env=None, timeout=None):
        return systemd.Result(["sh", "-c", cmd], 5, "", "pre failed")
    monkeypatch.setattr(ur, "_run_shell", fake_shell)

    rc = ur.run_upgrade("app1")

    assert rc == 3
    # serve must not have been stopped or started
    assert _calls(fake_systemd, "stop_and_wait") == []
    assert _calls(fake_systemd, "start") == []
    st = state.read_state("app1")
    assert st["status"] == "failed"
    assert st.get("abort_reason") == "pre_upgrade_hook"


def test_health_check_retries_until_success(tmp_home, fake_systemd, monkeypatch):
    """health_check returns healthy once a 0 exit code is observed."""
    monkeypatch.setattr(ur.time, "sleep", lambda _: None)
    _add_app(tmp_home, health_check="curl localhost", health_check_retries=3, health_check_interval_sec=1)
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (0, "", "", 0.1))

    attempts = {"n": 0}

    def fake_shell(cmd, env=None, timeout=None):
        attempts["n"] += 1
        # first two attempts fail, third succeeds
        rc = 0 if attempts["n"] >= 3 else 1
        return systemd.Result(["sh", "-c", cmd], rc, "", "")
    monkeypatch.setattr(ur, "_run_shell", fake_shell)

    rc = ur.run_upgrade("app1")

    assert rc == 0
    st = state.read_state("app1")
    assert st["healthy"] == "true"
    assert st["health_check_attempts"] == 3


def test_health_check_failure_makes_run_fail(tmp_home, fake_systemd, monkeypatch):
    """health_check failing all retries → exit non-zero even if upgrade was ok."""
    monkeypatch.setattr(ur.time, "sleep", lambda _: None)
    _add_app(tmp_home, health_check="curl localhost", health_check_retries=2, health_check_interval_sec=1)
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (0, "", "", 0.1))
    monkeypatch.setattr(ur, "_run_shell", lambda cmd, env=None, timeout=None: systemd.Result(["sh", "-c", cmd], 1, "", "bad"))

    rc = ur.run_upgrade("app1")

    assert rc == 1
    st = state.read_state("app1")
    assert st["healthy"] == "false"
    assert st["success"] is False


def test_post_hook_receives_env_vars(tmp_home, fake_systemd, monkeypatch):
    """post_upgrade_hook is called with EASYSTEMD_* env vars injected."""
    _add_app(tmp_home, post_upgrade_hook="echo post")
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (0, "", "", 0.1))

    captured = {}

    def fake_shell(cmd, env=None, timeout=None):
        if "EASYSTEMD_NAME" in (env or {}):
            captured.update(env)
        return systemd.Result(["sh", "-c", cmd], 0, "", "")
    monkeypatch.setattr(ur, "_run_shell", fake_shell)

    ur.run_upgrade("app1")

    assert captured.get("EASYSTEMD_NAME") == "app1"
    assert captured.get("EASYSTEMD_UPGRADE_EXIT_CODE") == "0"
    assert captured.get("EASYSTEMD_HEALTHY") == "skipped"


def test_post_hook_healthy_false_when_health_check_fails(tmp_home, fake_systemd, monkeypatch):
    monkeypatch.setattr(ur.time, "sleep", lambda _: None)
    _add_app(tmp_home, health_check="x", post_upgrade_hook="echo post", health_check_retries=1, health_check_interval_sec=1)
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (0, "", "", 0.1))
    captured = {}

    def fake_shell(cmd, env=None, timeout=None):
        if "EASYSTEMD_NAME" in (env or {}):
            captured.update(env)
        return systemd.Result(["sh", "-c", cmd], 1, "", "fail")
    monkeypatch.setattr(ur, "_run_shell", fake_shell)

    ur.run_upgrade("app1")

    assert captured.get("EASYSTEMD_HEALTHY") == "false"


def test_missing_app_returns_nonzero(tmp_home, fake_systemd, monkeypatch):
    rc = ur.run_upgrade("does-not-exist")
    assert rc != 0
    assert rc == 2


def test_running_state_written_at_start(tmp_home, fake_systemd, monkeypatch):
    """State file is set to 'running' before the upgrade executes."""
    _add_app(tmp_home)
    seen = {}

    def spy(app):
        seen["state"] = state.read_state("app1")
        return (0, "", "", 0.1)
    monkeypatch.setattr(ur, "_run_upgrade_command", spy)
    monkeypatch.setattr(ur, "_run_shell", lambda cmd, env=None, timeout=None: systemd.Result(["sh", "-c", cmd], 0, "", ""))

    ur.run_upgrade("app1")

    assert seen["state"]["status"] == "running"
    assert seen["state"]["started_at"] is not None


def test_stdout_truncated_in_state(tmp_home, fake_systemd, monkeypatch):
    """Captured stdout is truncated to ~4000 chars in the state file."""
    _add_app(tmp_home)
    big = "x" * 20000
    monkeypatch.setattr(ur, "_run_upgrade_command", lambda app: (0, big, "", 0.1))
    monkeypatch.setattr(ur, "_run_shell", lambda cmd, env=None, timeout=None: systemd.Result(["sh", "-c", cmd], 0, "", ""))

    ur.run_upgrade("app1")

    st = state.read_state("app1")
    assert len(st["stdout"]) <= 5000  # 4000 + truncation marker overhead
    assert "truncated" in st["stdout"]
