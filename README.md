# unitenv

**Explain where an environment variable for a running systemd service came from.**

`unitenv` is a local Linux command-line diagnostic. Give it a service and one
variable name; it checks the service's main process environment and points to
matching assignments in the unit configuration and referenced environment
files. Values are hidden unless you explicitly ask to reveal the requested
value.

```console
$ unitenv explain myapp.service --key API_ENDPOINT
Unit:             myapp.service
State:            active/running
Main PID:         2418
Variable:         API_ENDPOINT
Process snapshot: present (value hidden)
Need daemon reload: no
Current assignments:
  - /etc/myapp/runtime.env:4 (EnvironmentFile=; matches process snapshot; file not newer than process start)
PassEnvironment:  requested name is not listed
Assessment:       present; its value matches 1 current assignment candidate(s), but a matching value does not prove a unique historical source
```

The command correlates systemd's loaded unit properties, the unit files
currently on disk, `EnvironmentFile=` contents, and the initial environment
snapshot exposed by `/proc`. These are separate views of a service's
environment; `unitenv` reports what it observed and labels uncertainty instead
of claiming an exact historical source.

## Install

`unitenv` requires Linux, Python 3.10 or later, systemd, and permission to read
the selected process's `/proc/<PID>/environ` file. It has no third-party Python
dependencies.

Install from a checkout with [pipx](https://pipx.pypa.io/):

```console
git clone https://github.com/jad-fahmi/unitenv.git
cd unitenv
pipx install .
```

Or install into the active Python environment:

```console
python3 -m pip install .
```

## Usage

```text
unitenv explain UNIT --key NAME [--json] [--reveal]
```

For example:

```console
unitenv explain api.service --key DATABASE_URL
unitenv explain api.service --key DATABASE_URL --json
unitenv explain api.service --key DATABASE_URL --reveal
```

`--reveal` prints the requested value when it is present in the process
snapshot. Treat that output as sensitive. Human-readable and JSON output both
hide values by default. The JSON format is intended for scripts and diagnostic
commands. It includes `schema_version` and contains
`process_snapshot.present`, the `current_assignments` candidate list, loaded
configuration indicators, an assessment, and warnings.
Without `--reveal`, the process value is omitted and
`process_snapshot.value_redacted` is `true` when the variable is present.
Each current assignment includes a value comparison when `unitenv` can parse it
exactly. `manager_environment.comparison` reports the same check for a simple
value passed through `PassEnvironment=`. Configured values themselves are never
included in the report.

Exit status:

| Code | Meaning |
| --- | --- |
| `0` | The variable is present in the main process's initial environment snapshot. |
| `2` | The unit could not be inspected, the process snapshot could not be read, or command usage is invalid. |
| `3` | The variable is absent from the main process's initial environment snapshot. |

The tool does not invoke `sudo`, ask systemd to start or change a unit, write
files, or make network requests while diagnosing a service.

## What it checks

- Queries the system systemd manager for the service's state, main PID, loaded
  environment-related properties, and whether the manager says a daemon reload
  is needed.
- Reads only the selected PID's NUL-delimited `/proc/<PID>/environ` snapshot.
- Inspects the unit fragment and drop-ins shown by `systemctl cat`, along with
  the current contents of referenced `EnvironmentFile=` files.
- When the unit uses `PassEnvironment=`, checks whether the requested name is
  present in the system manager environment.
- Reports `UnsetEnvironment=` and `PAMName=` when present because they affect
  how an environment is assembled.
- Redacts values by default. It does not save diagnostic output or send it to a
  service.

## Limits and interpretation

`unitenv` is a diagnostic aid, not a full systemd execution simulator.

- `/proc/<PID>/environ` represents the environment supplied when that process
  was executed. It does not reliably describe later changes made inside the
  application. The inspected PID is the unit's `MainPID`; a launcher or worker
  may have a different environment.
- Unit files and environment files are inspected as they exist now. They may
  have changed since systemd loaded the unit or started the process. A newer
  file timestamp and `NeedDaemonReload=yes` are clues, not proof of which bytes
  systemd used.
- A matching assignment is a **candidate source**, not proof of provenance.
  Multiple assignments can use the same name, environment-file values override
  `Environment=` values, and `UnsetEnvironment=` is applied last. Manager,
  PAM, and systemd-generated variables can also affect the result. Source
  discovery compares values only when it can parse them exactly; it does not
  emulate every systemd expansion or establish which bytes were used at launch.
- The first release targets active system services managed by the system
  manager. User-manager units, worker-process selection, and complete parsing
  of every systemd syntax edge case are outside its supported scope.

The source and precedence rules are described in the upstream
[systemd execution environment documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html).
The meaning and access restrictions of the process snapshot are described in
[`proc_pid_environ(5)`](https://man7.org/linux/man-pages/man5/proc_pid_environ.5.html).
Environment variables are not a secure secret store; systemd recommends its
credential mechanisms for passing sensitive data to services.

## Development

```console
python3 -m pip install -e .
unitenv --help
```

The package uses only the Python standard library. Contributions should keep
values redacted by default and preserve the distinction between observed facts
and inferred source candidates. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
