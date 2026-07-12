"""Per-app state file (``last-run.json``) with atomic writes.

Each managed app has a state file at
``$XDG_STATE_HOME/easystemd/<name>/last-run.json`` recording the latest upgrade
run: status, timestamps, captured stdout/stderr (truncated), exit code and
health check result. The full output always remains available via
``journalctl --user`` so we only keep a short excerpt here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .config import app_state_dir, atomic_write_text

MAX_OUTPUT_EXCERPT = 4000


def state_file_path(name: str) -> Path:
    return app_state_dir(name) / "last-run.json"


def _truncate(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= MAX_OUTPUT_EXCERPT:
        return text
    half = MAX_OUTPUT_EXCERPT // 2
    return (
        text[:half]
        + f"\n...[truncated {len(text) - MAX_OUTPUT_EXCERPT} chars; full output in journalctl]...\n"
        + text[-half:]
    )


def read_state(name: str) -> Optional[dict[str, Any]]:
    """Read the state dict for ``name``; return ``None`` if no state file yet."""
    path = state_file_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"could not read state: {e}"}


def write_state(name: str, data: dict[str, Any]) -> None:
    """Atomically write ``data`` as JSON to the app's state file.

    Truncates any ``stdout``/``stderr`` fields to :data:`MAX_OUTPUT_EXCERPT`
    characters. Ensures the per-app state directory exists.
    """
    payload = dict(data)
    for key in ("stdout", "stderr"):
        if key in payload:
            payload[key] = _truncate(payload[key])
    path = state_file_path(name)
    body = json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n"
    atomic_write_text(path, body)


def initial_running_state(name: str) -> dict[str, Any]:
    """Build the initial "running" state record written at upgrade start."""
    from datetime import datetime, timezone

    return {
        "name": name,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "duration_s": None,
        "upgrade_exit_code": None,
        "stdout": None,
        "stderr": None,
        "healthy": None,
        "success": None,
    }
