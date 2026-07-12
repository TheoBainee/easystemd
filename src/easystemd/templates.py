"""Jinja2 rendering of systemd unit files.

All paths passed into templates are already absolute (binary, easystemd_exe,
working_dir, env_file are validated/resolved upstream). The renderer is pure:
given an :class:`AppConfig` and an absolute ``easystemd_exe`` path it returns
the three unit bodies as strings — no I/O.
"""

from __future__ import annotations

import os
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .models import AppConfig

_env = Environment(
    loader=PackageLoader("easystemd", "templates"),
    autoescape=select_autoescape([]),
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)


def _ctx(app: AppConfig, easystemd_exe: str) -> dict[str, object]:
    return {
        "name": app.name,
        "binary": app.binary,
        "serve_args": app.serve_args,
        "upgrade_args": app.upgrade_args,
        "exec_type": app.exec_type.value,
        "schedule": app.schedule,
        "working_dir": app.working_dir,
        "env_file": app.env_file,
        "restart_sec": app.restart_sec,
        "stop_timeout": app.stop_timeout,
        "randomized_delay": app.randomized_delay,
        "persistent": app.persistent,
        "easystemd_exe": easystemd_exe,
    }


def render_serve(app: AppConfig, easystemd_exe: str) -> str:
    return _env.get_template("serve.service.j2").render(**_ctx(app, easystemd_exe))


def render_upgrade(app: AppConfig, easystemd_exe: str) -> str:
    return _env.get_template("upgrade.service.j2").render(**_ctx(app, easystemd_exe))


def render_timer(app: AppConfig, easystemd_exe: str) -> str:
    return _env.get_template("upgrade.timer.j2").render(**_ctx(app, easystemd_exe))


def render_all(app: AppConfig, easystemd_exe: str) -> dict[str, str]:
    """Return ``{unit_filename: rendered_body}`` for the three units."""
    return {
        app.serve_unit: render_serve(app, easystemd_exe),
        app.upgrade_unit: render_upgrade(app, easystemd_exe),
        app.timer_unit: render_timer(app, easystemd_exe),
    }


def resolve_easystemd_exe() -> str:
    """Resolve the absolute path to the ``easystemd`` executable.

    Uses ``shutil.which`` first (the same one systemd will find if it is on the
    PATH of the user manager), then falls back to ``sys.argv[0]`` and the
    directory of ``sys.executable`` (covers venv/dev installs where the
    venv ``bin`` is not on PATH). The path embedded in the generated upgrade
    unit therefore never depends on systemd's (restricted) PATH.

    Raises if the executable cannot be located.
    """
    import shutil
    import sys

    candidates: list[str] = []
    found = shutil.which("easystemd")
    if found:
        candidates.append(found)
    if sys.argv and sys.argv[0]:
        candidates.append(sys.argv[0])
        candidates.append(str(Path(sys.argv[0]).resolve()))
    # directory of the running interpreter (venv bin / pipx venv bin)
    candidates.append(str(Path(sys.executable).parent / "easystemd"))

    seen: set[str] = set()
    for c in candidates:
        if not c:
            continue
        p = Path(c).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p)
        try:
            p = p.resolve()
        except OSError:
            continue
        if p in seen:
            continue
        seen.add(p)
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    raise FileNotFoundError(
        "could not locate the 'easystemd' executable on PATH, via sys.argv[0] "
        "or next to sys.executable; is it installed (e.g. via `pipx install .`)?"
    )
