"""Tests for config loading/saving and XDG path resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from easystemd import config as cfg
from easystemd.models import AppConfig, ConfigFile


def test_config_dir_uses_xdg(tmp_home):
    assert cfg.config_dir() == tmp_home / ".config" / "easystemd"


def test_state_dir_uses_xdg(tmp_home):
    assert cfg.state_dir() == tmp_home / ".local" / "state" / "easystemd"


def test_user_units_dir(tmp_home):
    assert cfg.user_units_dir() == tmp_home / ".config" / "systemd" / "user"


def test_load_config_returns_empty_when_missing(tmp_home):
    c = cfg.load_config()
    assert c.apps == []
    assert isinstance(c, ConfigFile)


def test_save_and_reload_roundtrip(tmp_home):
    app = AppConfig(name="mon-binaire", binary="/usr/local/bin/mon_binaire",
                    serve_args="web", upgrade_args="upgrade")
    cfg.add_app(app)
    reloaded = cfg.load_config()
    assert len(reloaded.apps) == 1
    assert reloaded.apps[0].name == "mon-binaire"
    assert reloaded.apps[0].binary == "/usr/local/bin/mon_binaire"
    assert reloaded.apps[0].schedule == "Sun 04:00:00"
    assert reloaded.apps[0].exec_type.value == "simple"


def test_add_app_duplicate_rejected(tmp_home):
    app = AppConfig(name="dup", binary="/usr/local/bin/x", serve_args="w", upgrade_args="u")
    cfg.add_app(app)
    with pytest.raises(cfg.ConfigError, match="already exists"):
        cfg.add_app(app)


def test_update_app_replaces(tmp_home):
    app = AppConfig(name="ed", binary="/usr/local/bin/x", serve_args="w", upgrade_args="u")
    cfg.add_app(app)
    app2 = AppConfig(name="ed", binary="/usr/local/bin/x", serve_args="prod", upgrade_args="u")
    cfg.update_app(app2)
    assert cfg.get_app("ed").serve_args == "prod"


def test_update_app_missing_errors(tmp_home):
    with pytest.raises(cfg.ConfigError, match="not found"):
        cfg.update_app(AppConfig(name="nope", binary="/x", serve_args="w", upgrade_args="u"))


def test_remove_app(tmp_home):
    cfg.add_app(AppConfig(name="rm", binary="/x", serve_args="w", upgrade_args="u"))
    cfg.remove_app("rm")
    assert cfg.get_app("rm") is None


def test_remove_app_missing_errors(tmp_home):
    with pytest.raises(cfg.ConfigError, match="not found"):
        cfg.remove_app("nope")


def test_config_file_has_apps_key_format(tmp_home):
    cfg.add_app(AppConfig(name="fmt", binary="/x", serve_args="w", upgrade_args="u"))
    text = cfg.config_file_path().read_text()
    assert "apps:" in text
    assert "- name: fmt" in text


def test_atomic_write_does_not_leave_tmp(tmp_home):
    cfg.add_app(AppConfig(name="a", binary="/x", serve_args="w", upgrade_args="u"))
    # no leftover .tmp files in the config dir
    tmps = list(cfg.config_dir().glob("*.tmp"))
    assert tmps == []


def test_invalid_yaml_raises_config_error(tmp_home, monkeypatch):
    p = cfg.config_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("apps: [this is not valid yaml   [[[")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config()


def test_invalid_app_data_raises_config_error(tmp_home):
    p = cfg.config_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("apps:\n  - name: BadName\n    binary: x\n    serve_args: w\n    upgrade_args: u\n")
    with pytest.raises(cfg.ConfigError):
        cfg.load_config()


def test_find_helper(tmp_home):
    a = AppConfig(name="find", binary="/x", serve_args="w", upgrade_args="u")
    cfg.add_app(a)
    cf = cfg.load_config()
    assert cf.find("find").binary == "/x"
    assert cf.find("missing") is None
    assert cf.names() == ["find"]


def test_multiple_apps_preserved(tmp_home):
    for n in ("a", "b", "c"):
        cfg.add_app(AppConfig(name=n, binary="/x", serve_args="w", upgrade_args="u"))
    cf = cfg.load_config()
    assert cf.names() == ["a", "b", "c"]


# ---- pydantic model validation (section 11) ------------------------------


def test_appconfig_defaults():
    a = AppConfig(name="x", binary="/usr/local/bin/x", serve_args="w", upgrade_args="u")
    assert a.exec_type.value == "simple"
    assert a.schedule == "Sun 04:00:00"
    assert a.restart_sec == 5
    assert a.stop_timeout == 30
    assert a.randomized_delay == 300
    assert a.persistent is True
    assert a.health_check_retries == 5
    assert a.health_check_interval_sec == 3
    assert a.env_file is None
    assert a.pre_upgrade_hook is None
    assert a.post_upgrade_hook is None


@pytest.mark.parametrize("bad_name", ["Bad_Name", "UPPER", "with space", "with.dot", "-leading", "trailing-", "", "café"])
def test_appconfig_rejects_invalid_name(bad_name):
    with pytest.raises(Exception):
        AppConfig(name=bad_name, binary="/x", serve_args="w", upgrade_args="u")


def test_appconfig_rejects_relative_binary():
    with pytest.raises(Exception):
        AppConfig(name="x", binary="relative/path", serve_args="w", upgrade_args="u")


def test_appconfig_rejects_empty_serve_args():
    with pytest.raises(Exception):
        AppConfig(name="x", binary="/x", serve_args="   ", upgrade_args="u")


def test_appconfig_rejects_empty_upgrade_args():
    with pytest.raises(Exception):
        AppConfig(name="x", binary="/x", serve_args="w", upgrade_args="   ")


def test_appconfig_rejects_negative_restart_sec():
    with pytest.raises(Exception):
        AppConfig(name="x", binary="/x", serve_args="w", upgrade_args="u", restart_sec=-1)


def test_appconfig_exec_type_enum():
    from easystemd.models import ExecType

    a = AppConfig(name="x", binary="/x", serve_args="w", upgrade_args="u", exec_type=ExecType.notify)
    assert a.exec_type == ExecType.notify

