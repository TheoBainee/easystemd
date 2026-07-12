"""easystemd CLI (typer).

Commands: add, edit, remove, list, status, upgrade-now, logs, doctor and the
hidden internal ``_run-upgrade`` invoked by the generated upgrade unit.

Errors are surfaced as clean messages by default; full tracebacks only with
``--debug``. Nothing requiring sudo is ever run silently.
"""

from __future__ import annotations

import json as _json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from . import config as cfg
from . import doctor as doctor_mod
from . import state
from . import systemd
from . import templates
from . import upgrade_runner
from .models import AppConfig, ConfigFile, ExecType

app = typer.Typer(
    name="easystemd",
    help="Automate systemd --user 'serve + scheduled upgrade' for standalone binaries.",
    no_args_is_help=True,
    add_completion=False,
)

_DEBUG = False


def _set_debug(debug: bool) -> None:
    global _DEBUG
    _DEBUG = debug


def _err(msg: str) -> None:
    typer.secho(msg, err=True, fg=typer.colors.RED)


def _ok(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.GREEN)


def _warn(msg: str) -> None:
    typer.secho(msg, err=True, fg=typer.colors.YELLOW)


def _handle_exc(e: Exception) -> None:
    if isinstance(e, cfg.ConfigError):
        _err(f"config error: {e}")
        raise typer.Exit(2)
    if isinstance(e, systemd.SystemdUnavailableError):
        _err(f"systemd --user unavailable: {e}")
        raise typer.Exit(3)
    if isinstance(e, systemd.SystemdError):
        _err(f"systemd error: {e}")
        raise typer.Exit(4)
    if isinstance(e, typer.Exit):
        raise e
    if _DEBUG:
        raise
    _err(f"error: {e}")
    raise typer.Exit(1)


@app.callback()
def main_callback(
    debug: bool = typer.Option(False, "--debug", help="Show full tracebacks on error."),
) -> None:
    _set_debug(debug)


# ---- helpers --------------------------------------------------------------


def _resolve_binary(binary: str) -> str:
    """Resolve ``--binary`` to an absolute, existing, executable path."""
    p = Path(binary)
    if not p.is_absolute():
        found = shutil.which(binary)
        if not found:
            raise typer.BadParameter(
                f"binary {binary!r} not found on PATH; pass an absolute path instead."
            )
        p = Path(found)
    p = p.resolve()
    if not p.exists():
        raise typer.BadParameter(f"binary does not exist: {p}")
    if not os.access(p, os.X_OK):
        raise typer.BadParameter(f"binary is not executable: {p}")
    return str(p)


def _resolve_optional_path(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise typer.BadParameter(f"path must be absolute: {value!r}")
    return str(p)


def _validate_schedule(schedule: str) -> str:
    res = systemd.validate_calendar(schedule)
    if not res.ok:
        raise typer.BadParameter(
            f"invalid schedule {schedule!r}: {res.stderr.strip() or res.stdout.strip()}"
        )
    return schedule


def _write_units_and_verify(app_cfg: AppConfig, easystemd_exe: str) -> list[Path]:
    """Render, write, verify. On verify failure, roll back written units and exit."""
    rendered = templates.render_all(app_cfg, easystemd_exe)
    written: list[Path] = []
    try:
        for unit, body in rendered.items():
            written.append(systemd.write_unit(unit, body))
        res = systemd.verify_units(written)
        if not res.ok:
            _err(f"generated units failed systemd-analyze verify:\n{res.stderr.strip() or res.stdout.strip()}")
            for p in written:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            raise typer.Exit(5)
    except typer.Exit:
        raise
    except Exception as e:
        # roll back on any unexpected error
        for p in written:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        raise
    return written


def _enable_units(app_cfg: AppConfig, enable_now: bool) -> None:
    """Enable the serve (with --now) and the timer."""
    if enable_now:
        r = systemd.enable(app_cfg.serve_unit, now=True)
        if not r.ok:
            _warn(f"could not enable+start serve {app_cfg.serve_unit}: {r.stderr.strip()}")
    r = systemd.enable(app_cfg.timer_unit, now=enable_now)
    if not r.ok:
        _warn(f"could not enable timer {app_cfg.timer_unit}: {r.stderr.strip()}")


# ---- add ------------------------------------------------------------------


@app.command()
def add(
    name: str = typer.Option(..., "--name", help="Unique slug [a-z0-9-]+."),
    binary: str = typer.Option(..., "--binary", help="Binary path or name (resolved via PATH)."),
    serve_args: str = typer.Option(..., "--serve-args", help='Subcommand for serve, e.g. "web".'),
    upgrade_args: str = typer.Option(..., "--upgrade-args", help='Subcommand for upgrade, e.g. "upgrade".'),
    schedule: str = typer.Option("Sun 04:00:00", "--schedule", help="OnCalendar expression."),
    working_dir: Optional[str] = typer.Option(None, "--working-dir", help="WorkingDirectory= (absolute)."),
    env_file: Optional[str] = typer.Option(None, "--env-file", help="EnvironmentFile= (absolute)."),
    exec_type: ExecType = typer.Option(ExecType.simple, "--exec-type", help="systemd Type=."),
    restart_sec: int = typer.Option(5, "--restart-sec", min=0),
    stop_timeout: int = typer.Option(30, "--stop-timeout", min=1),
    randomized_delay: int = typer.Option(300, "--randomized-delay", min=0),
    persistent: bool = typer.Option(True, "--persistent/--no-persistent"),
    pre_upgrade_hook: Optional[str] = typer.Option(None, "--pre-upgrade-hook"),
    post_upgrade_hook: Optional[str] = typer.Option(None, "--post-upgrade-hook"),
    health_check: Optional[str] = typer.Option(None, "--health-check"),
    health_check_retries: int = typer.Option(5, "--health-check-retries", min=1),
    health_check_interval: int = typer.Option(3, "--health-check-interval", min=1),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print units without writing/activating."),
    enable_now: bool = typer.Option(True, "--enable-now/--no-enable-now"),
) -> None:
    """Register a new app and generate + activate its systemd --user units."""
    try:
        if cfg.get_app(name) is not None:
            _err(f"app {name!r} already exists. Use `easystemd edit {name}` to modify it.")
            raise typer.Exit(2)

        binary_abs = _resolve_binary(binary)
        _validate_schedule(schedule)
        wd = _resolve_optional_path(working_dir) if working_dir else str(Path.home())
        ef = _resolve_optional_path(env_file)

        app_cfg = AppConfig(
            name=name,
            binary=binary_abs,
            serve_args=serve_args,
            upgrade_args=upgrade_args,
            exec_type=exec_type,
            schedule=schedule,
            working_dir=wd,
            env_file=ef,
            restart_sec=restart_sec,
            stop_timeout=stop_timeout,
            randomized_delay=randomized_delay,
            persistent=persistent,
            pre_upgrade_hook=pre_upgrade_hook,
            post_upgrade_hook=post_upgrade_hook,
            health_check=health_check,
            health_check_retries=health_check_retries,
            health_check_interval_sec=health_check_interval,
        )

        easystemd_exe = templates.resolve_easystemd_exe()

        if dry_run:
            rendered = templates.render_all(app_cfg, easystemd_exe)
            for unit, body in rendered.items():
                typer.secho(f"--- {unit} ---", fg=typer.colors.CYAN)
                typer.echo(body)
            return

        cfg.add_app(app_cfg)
        _write_units_and_verify(app_cfg, easystemd_exe)
        systemd.daemon_reload()
        _enable_units(app_cfg, enable_now)
        _ok(f"app {name!r} added; serve+timer enabled.")
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as e:
        _handle_exc(e)


# ---- edit -----------------------------------------------------------------


@app.command()
def edit(
    name: str = typer.Argument(..., help="App name to edit."),
    binary: Optional[str] = typer.Option(None, "--binary"),
    serve_args: Optional[str] = typer.Option(None, "--serve-args"),
    upgrade_args: Optional[str] = typer.Option(None, "--upgrade-args"),
    schedule: Optional[str] = typer.Option(None, "--schedule"),
    working_dir: Optional[str] = typer.Option(None, "--working-dir"),
    env_file: Optional[str] = typer.Option(None, "--env-file"),
    exec_type: Optional[ExecType] = typer.Option(None, "--exec-type"),
    restart_sec: Optional[int] = typer.Option(None, "--restart-sec", min=0),
    stop_timeout: Optional[int] = typer.Option(None, "--stop-timeout", min=1),
    randomized_delay: Optional[int] = typer.Option(None, "--randomized-delay", min=0),
    persistent: Optional[bool] = typer.Option(None, "--persistent/--no-persistent"),
    pre_upgrade_hook: Optional[str] = typer.Option(None, "--pre-upgrade-hook"),
    post_upgrade_hook: Optional[str] = typer.Option(None, "--post-upgrade-hook"),
    health_check: Optional[str] = typer.Option(None, "--health-check"),
    health_check_retries: Optional[int] = typer.Option(None, "--health-check-retries", min=1),
    health_check_interval: Optional[int] = typer.Option(None, "--health-check-interval", min=1),
) -> None:
    """Modify an existing app; regenerate units, daemon-reload, restart if needed."""
    try:
        existing = cfg.get_app(name)
        if existing is None:
            _err(f"app {name!r} not found. Use `easystemd add` to create it.")
            raise typer.Exit(2)

        data = existing.model_dump()
        if binary is not None:
            data["binary"] = _resolve_binary(binary)
        if serve_args is not None:
            data["serve_args"] = serve_args
        if upgrade_args is not None:
            data["upgrade_args"] = upgrade_args
        if schedule is not None:
            _validate_schedule(schedule)
            data["schedule"] = schedule
        if working_dir is not None:
            data["working_dir"] = _resolve_optional_path(working_dir)
        if env_file is not None:
            data["env_file"] = _resolve_optional_path(env_file)
        if exec_type is not None:
            data["exec_type"] = exec_type
        if restart_sec is not None:
            data["restart_sec"] = restart_sec
        if stop_timeout is not None:
            data["stop_timeout"] = stop_timeout
        if randomized_delay is not None:
            data["randomized_delay"] = randomized_delay
        if persistent is not None:
            data["persistent"] = persistent
        if pre_upgrade_hook is not None:
            data["pre_upgrade_hook"] = pre_upgrade_hook
        if post_upgrade_hook is not None:
            data["post_upgrade_hook"] = post_upgrade_hook
        if health_check is not None:
            data["health_check"] = health_check
        if health_check_retries is not None:
            data["health_check_retries"] = health_check_retries
        if health_check_interval is not None:
            data["health_check_interval_sec"] = health_check_interval

        new_cfg = AppConfig.model_validate(data)
        easystemd_exe = templates.resolve_easystemd_exe()

        was_active = systemd.is_active(new_cfg.serve_unit)
        cfg.update_app(new_cfg)
        _write_units_and_verify(new_cfg, easystemd_exe)
        systemd.daemon_reload()
        # restart the serve if it was running, so it picks up new config
        if was_active:
            systemd.start(new_cfg.serve_unit)
        # ensure timer is enabled
        if not systemd.is_enabled(new_cfg.timer_unit):
            systemd.enable(new_cfg.timer_unit, now=False)
        _ok(f"app {name!r} updated; units regenerated.")
    except (typer.Exit, typer.BadParameter):
        raise
    except Exception as e:
        _handle_exc(e)


# ---- remove ---------------------------------------------------------------


@app.command()
def remove(
    name: str = typer.Argument(..., help="App name to remove."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Stop, disable, delete units and remove the app from config."""
    try:
        app_cfg = cfg.get_app(name)
        if app_cfg is None:
            _err(f"app {name!r} not found.")
            raise typer.Exit(2)
        if not yes:
            confirm = typer.confirm(
                f"Remove {name!r}? This will stop+disable its units, delete the unit files and remove it from config.",
                default=False,
            )
            if not confirm:
                _warn("aborted.")
                raise typer.Exit(1)

        # stop + disable everything (best effort; tolerate already-stopped)
        for unit in app_cfg.all_units:
            systemd.disable(unit, now=True)
            systemd.reset_failed(unit)
        for unit in app_cfg.all_units:
            systemd.remove_unit(unit)
        cfg.remove_app(name)
        systemd.daemon_reload()
        # remove state dir
        try:
            import shutil as _sh

            sd = cfg.app_state_dir(name)
            if sd.exists():
                _sh.rmtree(sd)
        except Exception:
            pass
        _ok(f"app {name!r} removed.")
    except (typer.Exit,):
        raise
    except Exception as e:
        _handle_exc(e)


# ---- list -----------------------------------------------------------------


def _gather_list_row(app_cfg: AppConfig) -> dict:
    serve_active = systemd.is_active(app_cfg.serve_unit)
    nxt = None
    try:
        nxt = systemd.next_elapse(app_cfg.timer_unit)
    except Exception:
        pass
    last = state.read_state(app_cfg.name) or {}
    return {
        "name": app_cfg.name,
        "serve": "active" if serve_active else "inactive",
        "next_upgrade": nxt,
        "last_upgrade_at": last.get("finished_at"),
        "last_success": last.get("success"),
        "last_exit_code": last.get("upgrade_exit_code"),
    }


@app.command("list")
def list_cmd(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List all managed apps with serve status and last upgrade result."""
    try:
        cfg_file = cfg.load_config()
        rows = [_gather_list_row(a) for a in cfg_file.apps]
        if json_output:
            typer.echo(_json.dumps(rows, indent=2, default=str))
            return
        if not rows:
            typer.echo("No apps configured. Use `easystemd add` to create one.")
            return
        try:
            from rich.console import Console
            from rich.table import Table

            table = Table(title="easystemd apps")
            table.add_column("name")
            table.add_column("serve")
            table.add_column("next upgrade")
            table.add_column("last upgrade")
            table.add_column("result")
            for r in rows:
                last = r["last_upgrade_at"]
                if last:
                    last_short = str(last).replace("T", " ").split(".")[0]
                else:
                    last_short = "-"
                result = (
                    "ok" if r["last_success"] is True
                    else "failed" if r["last_success"] is False
                    else "-"
                )
                table.add_row(
                    r["name"],
                    r["serve"],
                    str(r["next_upgrade"] or "-"),
                    last_short,
                    result,
                )
            Console().print(table)
        except Exception:
            # fallback plain table
            typer.echo(f"{'name':<20} {'serve':<10} {'next':<24} {'last':<24} {'result'}")
            for r in rows:
                typer.echo(
                    f"{r['name']:<20} {r['serve']:<10} {str(r['next_upgrade'] or '-'):<24} "
                    f"{str(r['last_upgrade_at'] or '-'):<24} {r['last_success']}"
                )
    except (typer.Exit,):
        raise
    except Exception as e:
        _handle_exc(e)


# ---- status ---------------------------------------------------------------


@app.command()
def status(
    name: str = typer.Argument(..., help="App name."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show full detail for an app: systemctl status + last-run state."""
    try:
        app_cfg = cfg.get_app(name)
        if app_cfg is None:
            _err(f"app {name!r} not found.")
            raise typer.Exit(2)
        serve_status = systemd.unit_status(app_cfg.serve_unit)
        upgrade_status = systemd.unit_status(app_cfg.upgrade_unit)
        timer_status = systemd.unit_status(app_cfg.timer_unit)
        last = state.read_state(name)
        if json_output:
            typer.echo(
                _json.dumps(
                    {
                        "name": name,
                        "serve_unit": app_cfg.serve_unit,
                        "upgrade_unit": app_cfg.upgrade_unit,
                        "timer_unit": app_cfg.timer_unit,
                        "serve_active": systemd.is_active(app_cfg.serve_unit),
                        "timer_enabled": systemd.is_enabled(app_cfg.timer_unit),
                        "serve_status_rc": serve_status.returncode,
                        "serve_status": serve_status.stdout,
                        "upgrade_status": upgrade_status.stdout,
                        "timer_status": timer_status.stdout,
                        "last_run": last,
                    },
                    indent=2,
                    default=str,
                )
            )
            return
        typer.secho(f"=== {name} ===", fg=typer.colors.CYAN, bold=True)
        typer.secho("-- serve --", fg=typer.colors.YELLOW)
        typer.echo(serve_status.stdout.strip() or "(no status)")
        typer.secho("-- upgrade --", fg=typer.colors.YELLOW)
        typer.echo(upgrade_status.stdout.strip() or "(no status)")
        typer.secho("-- timer --", fg=typer.colors.YELLOW)
        typer.echo(timer_status.stdout.strip() or "(no status)")
        typer.secho("-- last run --", fg=typer.colors.YELLOW)
        if last:
            typer.echo(_json.dumps(last, indent=2, default=str))
        else:
            typer.echo("(no upgrade run yet)")
    except (typer.Exit,):
        raise
    except Exception as e:
        _handle_exc(e)


# ---- upgrade-now ----------------------------------------------------------


@app.command("upgrade-now")
def upgrade_now(
    name: str = typer.Argument(..., help="App name."),
    wait: bool = typer.Option(False, "--wait", help="Block until the upgrade service finishes."),
) -> None:
    """Trigger the upgrade service immediately."""
    try:
        app_cfg = cfg.get_app(name)
        if app_cfg is None:
            _err(f"app {name!r} not found.")
            raise typer.Exit(2)
        r = systemd.start(app_cfg.upgrade_unit)
        if not r.ok:
            _err(f"could not start {app_cfg.upgrade_unit}: {r.stderr.strip()}")
            raise typer.Exit(4)
        typer.echo(f"upgrade triggered for {name!r}.")
        if not wait:
            return
        # oneshot: wait until the unit is no longer active
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            if not systemd.is_active(app_cfg.upgrade_unit):
                break
            time.sleep(1)
        last = state.read_state(name)
        if last:
            success = last.get("success")
            typer.secho(
                f"upgrade finished: success={success} exit_code={last.get('upgrade_exit_code')} healthy={last.get('healthy')}",
                fg=typer.colors.GREEN if success else typer.colors.RED,
            )
        else:
            typer.echo("upgrade finished (no state recorded).")
    except (typer.Exit,):
        raise
    except Exception as e:
        _handle_exc(e)


# ---- logs -----------------------------------------------------------------


@app.command()
def logs(
    name: str = typer.Argument(..., help="App name."),
    serve: bool = typer.Option(False, "--serve", help="Show serve logs (default: upgrade logs)."),
    upgrade: bool = typer.Option(False, "--upgrade", help="Show upgrade logs (default)."),
    follow: bool = typer.Option(False, "--follow", help="Follow new lines."),
) -> None:
    """Tail journalctl --user for an app's serve or upgrade unit.

    Without ``--serve`` or ``--upgrade``, defaults to the **upgrade** unit
    (prioritising upgrade failure diagnostics).
    """
    try:
        app_cfg = cfg.get_app(name)
        if app_cfg is None:
            _err(f"app {name!r} not found.")
            raise typer.Exit(2)
        if serve and not upgrade:
            unit = app_cfg.serve_unit
        else:
            unit = app_cfg.upgrade_unit
        cmd = ["journalctl", "--user", "-u", unit]
        if follow:
            cmd.append("-f")
        # hand off to journalctl so output streams live
        os.execvp(cmd[0], cmd)
    except (typer.Exit,):
        raise
    except Exception as e:
        _handle_exc(e)


# ---- doctor ---------------------------------------------------------------


@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Offer interactive fixes (never silent sudo)."),
) -> None:
    """Run environment and per-app diagnostics."""
    try:
        report = doctor_mod.run_doctor()
        for f in report.findings:
            color = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW, "error": typer.colors.RED}[f.level]
            typer.secho(f"[{f.level.upper():5}] {f.scope}/{f.check}: {f.message}", fg=color)
            if f.fix_hint:
                typer.secho(f"        fix: {f.fix_hint}", fg=typer.colors.CYAN)
        if fix and report.errors:
            linger_err = [f for f in report.errors if f.check == "linger"]
            if linger_err:
                hint = linger_err[0].fix_hint or systemd.linger_enable_command()
                typer.secho(f"To enable linger, run:\n  {hint}", fg=typer.colors.CYAN)
                if typer.confirm("Run it now via sudo? (never done silently)", default=False):
                    r = systemd.linger_enable()
                    if r.ok:
                        _ok("linger enabled.")
                    else:
                        _err(f"failed to enable linger: {r.stderr.strip()}")
        if report.ok:
            _ok("doctor: no errors.")
        else:
            typer.secho(f"doctor: {len(report.errors)} error(s), {len(report.warnings)} warning(s).", err=True, fg=typer.colors.RED)
            raise typer.Exit(1)
    except (typer.Exit,):
        raise
    except Exception as e:
        _handle_exc(e)


# ---- internal _run-upgrade (hidden) --------------------------------------


@app.command(name="_run-upgrade", hidden=True)
def run_upgrade_cmd(
    name: str = typer.Argument(..., help="App name (internal)."),
) -> None:
    """Internal entry point invoked by the generated upgrade unit's ExecStart."""
    try:
        rc = upgrade_runner.run_upgrade(name)
        raise typer.Exit(rc)
    except (typer.Exit,):
        raise
    except Exception as e:
        _handle_exc(e)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
