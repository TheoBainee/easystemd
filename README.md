# easystemd

Automate the **"serve + scheduled upgrade"** pattern for standalone binaries
using `systemd --user`.

If you run self-contained binaries that expose both a long-running server
command (e.g. `mybin web`) and a self-update command (e.g. `mybin upgrade`),
`easystemd` generates and manages the three `systemd --user` units needed to:

- keep the server running with auto-restart on crash (`Restart=always`),
- trigger the upgrade on a schedule (timer + `oneshot` service),
- **guarantee** the server is stopped before the upgrade and **always restarted
  afterwards — even if the upgrade fails or raises an exception.**

It is generic (no coupling to any particular binary), multi-instance (manage as
many binaries as you like, each with its own config/units/state), and
idempotent.

## Principle

For each registered app, `easystemd` generates three user units:

| Unit | Type | Role |
|---|---|---|
| `easystemd-<name>-serve.service` | `simple`/`exec`/… | Runs `<binary> <serve-args>` with `Restart=always` |
| `easystemd-<name>-upgrade.service` | `oneshot` | Runs `easystemd _run-upgrade <name>`, which stops the serve, runs `<binary> <upgrade-args>`, then **always** restarts the serve |
| `easystemd-<name>-upgrade.timer` | timer | Triggers the upgrade service on an `OnCalendar` schedule |

The upgrade service uses a `try`/`finally` structure: the `finally` block
**always** starts the serve service again, whatever happens in the upgrade
(exit code non-zero *or* an unhandled exception). This is the single most
important guarantee of the tool and is covered by the test suite.

## Installation

Recommended (isolated, puts `easystemd` on your PATH):

```bash
pipx install .
```

Alternative (user install):

```bash
pip install --user .
```

Then make sure `~/.local/bin` (or pipx's injected bin dir) is on your **shell**
PATH — `easystemd` resolves its own absolute path at `add` time so the
generated units never depend on systemd's restricted PATH (see
[The restricted PATH pitfall](#the-restricted-path-pitfall) below).

Dependencies: Python 3.11+, `typer`, `pydantic` v2, `PyYAML`, `Jinja2`, `rich`.
No root required.

## Quickstart

Using a fictional binary `mon_binaire` installed at `/usr/local/bin/mon_binaire`
that exposes `mon_binaire web` (server) and `mon_binaire upgrade` (self-update):

```bash
# 1. Register the app and generate + enable the three units
easystemd add \
  --name mon-binaire \
  --binary /usr/local/bin/mon_binaire \
  --serve-args "web" \
  --upgrade-args "upgrade" \
  --schedule "Sun 04:00" \
  --randomized-delay 300

# 2. List all managed apps (serve status, next upgrade, last result)
easystemd list

# 3. Detailed status of one app (systemctl status + last-run state)
easystemd status mon-binaire

# 4. Trigger an upgrade immediately and wait for it to finish
easystemd upgrade-now mon-binaire --wait

# 5. Tail the logs (default: upgrade unit; use --serve for the server)
easystemd logs mon-binaire --serve --follow

# 6. Check environment + per-app health
easystemd doctor

# 7. Remove an app (stop + disable + delete units + remove from config)
easystemd remove mon-binaire --yes
```

You can also pass a bare command name to `--binary` — it will be resolved via
`shutil.which()` then `Path.resolve()` to an absolute path.

### Real-world example: serving [opencode](https://opencode.ai)

`opencode` is a standalone binary that exposes `opencode web` (a long-running
web server) and `opencode upgrade` (a self-update command) — exactly the
pattern `easystemd` automates:

```bash
easystemd add \
  --name opencode-web \
  --binary opencode \
  --serve-args "web --mdns --port 12345 --hostname 0.0.0.0 --print-logs true" \
  --upgrade-args "upgrade" \
  --schedule "Mon 04:00" \
  --randomized-delay 5
```

This generates `easystemd-opencode-web-serve.service` (auto-restarted on crash),
`easystemd-opencode-web-upgrade.service` (stops the server, runs
`opencode upgrade`, always restarts the server) and
`easystemd-opencode-web-upgrade.timer` (fires every Monday at 04:00, ±5s random
delay). Check it with:

```bash
easystemd status opencode-web
easystemd logs opencode-web --serve --follow
```

## CLI reference

### `easystemd add`

Register a new app and generate + activate its units.

```
easystemd add --name NAME --binary PATH --serve-args STR --upgrade-args STR
              [--schedule CRON]            # default: "Sun 04:00:00"
              [--working-dir PATH]         # default: $HOME
              [--env-file PATH]
              [--exec-type simple|forking|exec|notify]   # default: simple
              [--restart-sec N]            # default: 5
              [--stop-timeout N]           # default: 30
              [--randomized-delay N]       # default: 300
              [--persistent/--no-persistent]   # default: persistent
              [--pre-upgrade-hook STR]
              [--post-upgrade-hook STR]
              [--health-check STR]         # shell command; exit 0 = healthy
              [--health-check-retries N]   # default: 5
              [--health-check-interval N]  # default: 3
              [--dry-run]                  # print units, write/enable nothing
              [--enable-now/--no-enable-now]   # default: enable-now
```

The `schedule` is validated with `systemd-analyze calendar` before anything is
written. Generated units are validated with `systemd-analyze verify`; if
verification fails, the written unit files are rolled back and the command
aborts (you never end up with invalid units enabled).

### `easystemd edit NAME`

Modify an existing app. Only the options you pass are changed; the rest is
preserved. Regenerates the units, runs `daemon-reload`, restarts the serve if
it was running, and re-enables the timer if needed.

Accepts the same options as `add` (minus `--dry-run`/`--enable-now`). Fails if
`NAME` is unknown — use `add` to create.

### `easystemd remove NAME [--yes]`

Stops + disables all three units, deletes the unit files, removes the app from
config and deletes its state directory. Asks for confirmation unless `--yes`.

### `easystemd list [--json]`

Table of all apps: name, serve status (active/inactive), next scheduled
upgrade, last upgrade timestamp and result (ok/failed). `--json` emits a JSON
array.

### `easystemd status NAME [--json]`

Full detail for one app: combined `systemctl --user status` for the three units
plus the contents of `last-run.json`. `--json` emits a single JSON object.

### `easystemd upgrade-now NAME [--wait]`

Triggers `systemctl --user start easystemd-<name>-upgrade.service` immediately.
With `--wait`, polls until the oneshot finishes and prints the result
(success / exit code / health).

### `easystemd logs NAME [--serve|--upgrade] [--follow]`

Wrapper around `journalctl --user -u <unit>`. Without `--serve` or `--upgrade`,
defaults to the **upgrade** unit (prioritising upgrade-failure diagnostics).
`--follow` keeps streaming.

### `easystemd doctor [--fix]`

Runs read-only diagnostics:

- **linger** enabled for the current user (otherwise shows the exact
  `sudo loginctl enable-linger $USER` command to run; with `--fix` it asks for
  explicit confirmation before running it via sudo — never silently).
- **user bus** reachable.
- per app: **binary** present and executable at its resolved path; **units**
  present on disk and coherent with the current config (detects a config edited
  without regenerating units); **timer** enabled with a next scheduled run.
- all on-disk units pass `systemd-analyze verify`.

Exits non-zero if any error is found. `--fix` only offers the interactive
linger fix; everything else is reported with a `fix:` hint you run yourself
(typically `easystemd edit <name>` to regenerate stale units).

### `easystemd _run-upgrade NAME` *(internal, hidden from `--help`)*

Invoked only by the generated upgrade unit's `ExecStart`. Loads the app config,
runs the full upgrade sequence (see [Principle](#principle)) and exits non-zero
if the upgrade or the health check failed. You do not call this directly.

### Global option

`easystemd --debug ...` shows full Python tracebacks on error (otherwise errors
are printed as clean one-line messages).

## Generated file layout

```
~/.config/easystemd/
└── config.yaml                          # all apps (YAML, atomic writes)
~/.config/systemd/user/
├── easystemd-<name>-serve.service       # generated per app
├── easystemd-<name>-upgrade.service
└── easystemd-<name>-upgrade.timer
~/.local/state/easystemd/
└── <name>/
    └── last-run.json                    # status, timestamps, exit code, truncated output
```

`config.yaml` format:

```yaml
apps:
  - name: mon-binaire
    binary: /usr/local/bin/mon_binaire
    serve_args: web
    upgrade_args: upgrade
    schedule: "Sun 04:00:00"
    exec_type: simple
    # ...all fields from the model
```

All file writes (config, units, state) are atomic: write to a temp file then
`os.replace()`.

## The upgrade sequence in detail

When `easystemd-<name>-upgrade.service` fires (via the timer or
`upgrade-now`), `easystemd _run-upgrade <name>`:

1. Loads and validates the app config (aborts if missing/invalid).
2. Writes a `running` state to `last-run.json`.
3. Runs `pre_upgrade_hook` if defined — **if it fails, aborts immediately**
   (the serve has not been touched, so no restart is needed).
4. `systemctl --user stop <serve>`, polling `is-active` up to `stop_timeout`.
5. **`try` / `finally`**: runs `<binary> <upgrade-args>`; the `finally`
   **always** runs `systemctl --user start <serve>` — even on a non-zero exit
   code or an unhandled Python exception.
6. Runs `health_check` with retries if defined (`healthy=true` on first exit 0).
7. Runs `post_upgrade_hook` if defined, with `EASYSTEMD_NAME`,
   `EASYSTEMD_UPGRADE_EXIT_CODE` and `EASYSTEMD_HEALTHY` (`true`/`false`/`skipped`)
   injected into its environment.
8. Writes the final state: `finished_at`, `duration_s`, `upgrade_exit_code`,
   `healthy`, `success`.
9. Exits non-zero if the upgrade failed **or** the health check failed after
   all retries — so `systemctl status` / `journalctl` / `easystemd list`
   reflect the failure.

No automatic rollback of the binary is performed (out of scope: not every
binary can downgrade). Failures are surfaced clearly instead.

## Troubleshooting

### My services stop when I log out / after a reboot

You need **linger** enabled so your user manager keeps running without an
active login session:

```bash
easystemd doctor
```

If linger is off, `doctor` prints the exact command to run:

```bash
sudo loginctl enable-linger $USER
```

`easystemd` never runs `sudo` silently. With `doctor --fix` it asks for explicit
confirmation first.

### `could not reach the systemd --user bus`

You're likely in a container without a user systemd, or
`DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR` are unset. `doctor` reports this
as a `user-bus` error. Run from a real login session (or `systemctl --user`
should work standalone first).

### The timer doesn't fire

Check `easystemd status <name>` (timer section) and `systemctl --user list-timers
'easystemd-*'`. If the timer isn't enabled, `easystemd edit <name>` regenerates
and re-enables it. `doctor` flags a disabled timer as a warning.

### Units are stale after I edited the config by hand

`easystemd doctor` reports `units ... differs from current config (stale unit)`
whenever the on-disk units no longer match what the current config would
generate. Fix with `easystemd edit <name>` (this regenerates the units from the
current config without changing any field).

### An upgrade failed but the server is down

That should not happen — the `finally` block always restarts the serve. If it
did, the `last-run.json` will show `serve_restarted: false` with a
`serve_restart_returncode`; inspect `journalctl --user -u easystemd-<name>-serve.service`.
`easystemd upgrade-now <name> --wait` will also re-run the upgrade (and restart
the serve).

## The restricted PATH pitfall

`systemd --user` services do **not** inherit your interactive shell's `PATH`.
In particular, if `easystemd` is installed via `pipx` or `pip --user`, its
executable lives in `~/.local/bin` (or a pipx venv), which is usually **not**
on the PATH the user systemd manager resolves.

To avoid units that fail with "command not found" at upgrade time, every
`ExecStart=` generated by `easystemd` uses **absolute paths**:

- the managed binary is resolved via `shutil.which()` then `Path.resolve()` at
  `add` time;
- the `easystemd _run-upgrade` invocation in the upgrade service uses the
  absolute path to the `easystemd` executable resolved at `add`/`edit` time.

So once `add` has succeeded, the units are self-contained and never depend on
systemd's PATH. If you later move the binary or `easystemd`, re-run
`easystemd edit <name>` to re-resolve the paths.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest                # unit tests (no real systemctl is invoked)
pytest -m integration # optional: requires a real systemd --user session
```

The test suite stubs all `subprocess` calls and isolates `$HOME` / XDG dirs, so
it never touches your real config or systemd.

## License

MIT
