"""Configuration loading/saving and XDG base directory resolution.

All file writes are atomic: write to a temp file in the same directory then
``os.replace()``. XDG Base Directories are honoured with the standard defaults.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from .models import AppConfig, ConfigFile


# ---- XDG path resolution --------------------------------------------------


def _xdg(env_var: str, default_subdir: str) -> Path:
    """Resolve an XDG base directory with its standard default.

    Honours ``$env_var``; falls back to ``$HOME/default_subdir``. Expands user
    and env vars. Never raises if HOME is unset (uses cwd as last resort).
    """
    raw = os.environ.get(env_var)
    if raw and raw.strip():
        return Path(raw).expanduser()
    home = os.environ.get("HOME") or str(Path.home())
    return Path(home) / default_subdir


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/easystemd`` (default ``~/.config/easystemd``)."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "easystemd"


def state_dir() -> Path:
    """``$XDG_STATE_HOME/easystemd`` (default ``~/.local/state/easystemd``)."""
    return _xdg("XDG_STATE_HOME", ".local/state") / "easystemd"


def user_units_dir() -> Path:
    """Where user systemd units live: ``~/.config/systemd/user``.

    Per systemd convention this is not overridable by XDG_CONFIG_HOME for the
    user manager; it is always ``$XDG_CONFIG_HOME/systemd/user``.
    """
    return _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def config_file_path() -> Path:
    return config_dir() / "config.yaml"


def app_state_dir(name: str) -> Path:
    return state_dir() / name


# ---- atomic write helper --------------------------------------------------


def atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Write ``content`` to ``path`` atomically.

    Writes to a named temp file in the same directory (so ``os.replace`` stays
    on the same filesystem) then atomically renames. Ensures parent dirs exist.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in same dir to guarantee same-filesystem rename.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- config load / save ---------------------------------------------------


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable or invalid."""


def load_config() -> ConfigFile:
    """Load and validate ``config.yaml``.

    Returns an empty :class:`ConfigFile` if the file does not exist (so callers
    can ``add_app`` on a fresh install). Raises :class:`ConfigError` on I/O or
    validation errors with a clear message.
    """
    path = config_file_path()
    if not path.exists():
        return ConfigFile(apps=[])
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")
    if "apps" not in data:
        data["apps"] = []
    try:
        return ConfigFile.model_validate(data)
    except Exception as e:
        raise ConfigError(f"invalid config in {path}: {e}") from e


def save_config(cfg: ConfigFile) -> None:
    """Serialise ``cfg`` to ``config.yaml`` atomically.

    Dumps with a leading comment pointing readers at the tool. App order is
    preserved (we dump the list as-is).
    """
    path = config_file_path()
    data = cfg.model_dump(mode="json")
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
    header = "# Managed by easystemd. Edit via `easystemd edit` to keep units in sync.\n"
    atomic_write_text(path, header + body)


# ---- per-app CRUD ---------------------------------------------------------


def get_app(name: str) -> Optional[AppConfig]:
    return load_config().find(name)


def add_app(app: AppConfig) -> None:
    """Insert ``app`` into config; error if ``name`` already exists."""
    cfg = load_config()
    if cfg.find(app.name) is not None:
        raise ConfigError(
            f"app {app.name!r} already exists; use `easystemd edit {app.name}` to modify it."
        )
    cfg.apps.append(app)
    save_config(cfg)


def update_app(app: AppConfig) -> None:
    """Replace the app with matching ``name``; error if missing."""
    cfg = load_config()
    for i, existing in enumerate(cfg.apps):
        if existing.name == app.name:
            cfg.apps[i] = app
            save_config(cfg)
            return
    raise ConfigError(f"app {app.name!r} not found; use `easystemd add` to create it.")


def remove_app(name: str) -> None:
    """Remove ``name`` from config; error if missing. Returns nothing."""
    cfg = load_config()
    for i, existing in enumerate(cfg.apps):
        if existing.name == name:
            del cfg.apps[i]
            save_config(cfg)
            return
    raise ConfigError(f"app {name!r} not found.")
