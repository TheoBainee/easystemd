"""The upgrade runner — the critical core executed by ``_run-upgrade NAME``.

Guarantees (per ProjectDescription section 7):

1. Load and validate config for ``NAME``. Absent/invalid → exit non-zero, nothing else.
2. Write ``running`` + ``started_at`` to the state file.
3. If ``pre_upgrade_hook`` is defined, run it. **If it fails, abandon immediately**
   (the serve service has not been touched yet, so no restart is needed) —
   record failure, exit non-zero.
4. ``systemctl --user stop <name>-serve.service`` with active wait (poll
   ``is-active`` up to ``stop_timeout``).
5. ``try`` / ``finally``:
   - *try*: run ``<binary> <upgrade_args>``, capture stdout/stderr (truncated in
     state), exit code, duration.
   - *finally*: **always** ``systemctl --user start <name>-serve.service``,
     whatever happens in the try block (including a Python exception).
6. If ``health_check`` defined: retry loop, ``healthy=True`` on first exit 0,
     else ``healthy=False`` after exhausting attempts.
7. If ``post_upgrade_hook`` defined: run it with ``EASYSYSTEMD_NAME``,
     ``EASYSYSTEMD_UPGRADE_EXIT_CODE`` and ``EASYSYSTEMD_HEALTHY``
     (``true``/``false``/``skipped``) injected into its environment.
8. Update the state file: ``finished_at``, ``duration_s``, ``upgrade_exit_code``,
     ``healthy``, ``success``.
9. Process exit code is non-zero if the upgrade failed OR the health check
     failed after all retries.

The serve service **always** gets restarted, even if the upgrade raises an
exception or returns non-zero — this is the single most important property and
is covered by tests.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from . import config as cfg
from . import state
from . import systemd
from .models import AppConfig


class UpgradeAbort(Exception):
    """Raised to abort the upgrade before the serve is touched (pre-hook failure)."""

    pass


# ---- shell helpers (kept module-level so tests can monkeypatch) ----------


def _run_shell(command: str, env: Optional[dict[str, str]] = None, timeout: Optional[int] = None) -> systemd.Result:
    """Run a shell command string, returning a :class:`systemd.Result`.

    Uses ``shell=True`` with the provided env (merged over ``os.environ``).
    Never raises on non-zero — caller inspects ``returncode``.
    """
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
        timeout=timeout,
    )
    return systemd.Result(
        cmd=["sh", "-c", command],
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _run_upgrade_command(app: AppConfig) -> tuple[Optional[int], str, str, Optional[float]]:
    """Run ``<binary> <upgrade_args>``. Returns (exit_code, stdout, stderr, duration_s).

    If the binary cannot be executed (e.g. disappeared), ``exit_code`` is
    ``None`` and stderr carries the error message — the caller still restarts
    the serve (handled by the try/finally in :func:`run_upgrade`).
    """
    import shlex as _shlex

    cmd = [app.binary, *_shlex.split(app.upgrade_args)]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        duration = time.monotonic() - start
        return proc.returncode, proc.stdout, proc.stderr, duration
    except Exception as e:
        duration = time.monotonic() - start
        return None, "", f"failed to execute upgrade command: {e}", duration


# ---- health check --------------------------------------------------------


def _run_health_check(app: AppConfig) -> tuple[bool, int]:
    """Run the health check with retries. Returns (healthy, attempts_used).

    ``healthy=True`` as soon as one attempt returns 0. After
    ``health_check_retries`` attempts, returns ``False``. If no health check is
    defined, returns ``(False, 0)`` with a sentinel meaning "skipped" handled by
    the caller via ``app.health_check is None``.
    """
    attempts = 0
    for i in range(app.health_check_retries):
        if i > 0:
            time.sleep(app.health_check_interval_sec)
        attempts += 1
        res = _run_shell(app.health_check)
        if res.returncode == 0:
            return True, attempts
    return False, attempts


# ---- main entry point ----------------------------------------------------


def _healthy_str(healthy: Optional[bool], has_check: bool) -> str:
    if not has_check:
        return "skipped"
    return "true" if healthy else "false"


def run_upgrade(name: str) -> int:
    """Execute the full upgrade sequence for ``name``. Returns the process exit code.

    This is the function invoked by the ``_run-upgrade`` CLI command. It never
    raises for "normal" failure modes — it records the failure in the state
    file and returns a non-zero exit code. Unexpected internal errors propagate
    only after the serve has been restarted (the finally block protects that).
    """
    # Step 1 — load + validate config.
    app = cfg.get_app(name)
    if app is None:
        print(f"easystemd: no app named {name!r} in config", file=sys.stderr)
        return 2
    try:
        # re-validate to be safe (config may have been hand-edited)
        AppConfig.model_validate(app.model_dump())
    except Exception as e:
        print(f"easystemd: config for {name!r} is invalid: {e}", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()

    # Step 2 — write running state.
    running = state.initial_running_state(name)
    state.write_state(name, running)

    has_health = app.health_check is not None and app.health_check.strip() != ""

    # Step 3 — pre-upgrade hook. Failure aborts BEFORE serve is touched.
    if app.pre_upgrade_hook and app.pre_upgrade_hook.strip():
        pre = _run_shell(app.pre_upgrade_hook)
        if pre.returncode != 0:
            state.write_state(name, {
                **running,
                "status": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_s": time.monotonic() - started_monotonic,
                "upgrade_exit_code": None,
                "stdout": pre.stdout,
                "stderr": f"pre_upgrade_hook failed (rc={pre.returncode}):\n{pre.stderr}",
                "healthy": None,
                "success": False,
                "abort_reason": "pre_upgrade_hook",
            })
            print(
                f"easystemd: pre_upgrade_hook for {name!r} failed (rc={pre.returncode}); aborting before serve stop.",
                file=sys.stderr,
            )
            return 3

    # Step 4 — stop the serve, polling until it's really down.
    systemd.stop_and_wait(app.serve_unit, app.stop_timeout)

    upgrade_exit_code: Optional[int] = None
    stdout = ""
    stderr = ""
    duration_upgrade: Optional[float] = None
    serve_restart_result: Optional[systemd.Result] = None

    # Steps 5 — try/except/finally: upgrade then ALWAYS restart serve.
    # `except Exception` (not BaseException) so KeyboardInterrupt/SystemExit
    # still propagate after the serve has been restarted by finally.
    try:
        upgrade_exit_code, stdout, stderr, duration_upgrade = _run_upgrade_command(app)
    except Exception as e:
        # Any unexpected error during the upgrade execution itself (e.g. a test
        # monkeypatching the command runner to raise). Record it as a failure;
        # the finally block still restarts the serve, then we continue to the
        # health check / post-hook / state recording with a clean exit code.
        stderr = f"internal error during upgrade: {e!r}"
        upgrade_exit_code = None
        duration_upgrade = time.monotonic() - started_monotonic
    finally:
        # THE critical guarantee: serve is (re)started no matter what.
        try:
            serve_restart_result = systemd.start(app.serve_unit)
        except BaseException as restart_err:
            # Even the restart itself failed — record it so the state reflects
            # reality, but do not swallow the original upgrade exception.
            print(
                f"easystemd: FAILED to restart serve {app.serve_unit}: {restart_err}",
                file=sys.stderr,
            )
            serve_restart_result = systemd.Result(
                cmd=["systemctl", "--user", "start", app.serve_unit],
                returncode=-1,
                stdout="",
                stderr=str(restart_err),
            )

    # Step 6 — health check (serve is back up now).
    healthy: Optional[bool] = None
    health_attempts = 0
    if has_health:
        healthy, health_attempts = _run_health_check(app)

    # Step 7 — post-upgrade hook with injected env vars.
    post_result: Optional[systemd.Result] = None
    if app.post_upgrade_hook and app.post_upgrade_hook.strip():
        post_env = {
            "EASYSTEMD_NAME": name,
            "EASYSTEMD_UPGRADE_EXIT_CODE": str(upgrade_exit_code) if upgrade_exit_code is not None else "none",
            "EASYSTEMD_HEALTHY": _healthy_str(healthy, has_health),
        }
        post_result = _run_shell(app.post_upgrade_hook, env=post_env)

    # Step 8 — final state.
    finished_at = datetime.now(timezone.utc)
    total_duration = time.monotonic() - started_monotonic
    upgrade_failed = upgrade_exit_code is None or upgrade_exit_code != 0
    health_failed = has_health and healthy is False
    success = (not upgrade_failed) and (not health_failed)

    final_state = {
        **running,
        "status": "success" if success else "failed",
        "finished_at": finished_at.isoformat(),
        "duration_s": total_duration,
        "upgrade_exit_code": upgrade_exit_code,
        "upgrade_duration_s": duration_upgrade,
        "stdout": stdout,
        "stderr": stderr,
        "healthy": _healthy_str(healthy, has_health),
        "health_check_attempts": health_attempts,
        "success": success,
        "serve_restarted": serve_restart_result is not None and serve_restart_result.returncode == 0,
        "serve_restart_returncode": serve_restart_result.returncode if serve_restart_result else None,
    }
    if post_result is not None:
        final_state["post_upgrade_hook"] = {
            "returncode": post_result.returncode,
            "stdout": post_result.stdout,
            "stderr": post_result.stderr,
        }
    state.write_state(name, final_state)

    # Step 9 — exit code: non-zero if upgrade OR health check failed.
    if upgrade_failed or health_failed:
        return 1
    return 0
