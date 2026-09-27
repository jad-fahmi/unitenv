"""Small, read-only adapters for systemctl and procfs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path



class InspectionError(RuntimeError):
    """An expected error while inspecting the local system."""


@dataclass(frozen=True)
class Unit:
    name: str
    load_state: str
    active_state: str
    sub_state: str
    main_pid: int
    need_daemon_reload: bool | None
    properties: dict[str, str]


_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "NeedDaemonReload",
    "FragmentPath",
    "DropInPaths",
    "Environment",
    "EnvironmentFiles",
    "PassEnvironment",
    "UnsetEnvironment",
    "PAMName",
)
_SAFE_MANAGER_VALUE = re.compile(r"^[A-Za-z0-9_.,:/@%+=-]*$")


def _systemctl(*arguments: str, timeout: float = 15.0) -> str:
    executable = shutil.which("systemctl")
    if executable is None:
        raise InspectionError("systemctl was not found; unitenv requires systemd")
    try:
        result = subprocess.run(
            [executable, "--system", *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise InspectionError("systemctl timed out while inspecting the unit") from exc
    except OSError as exc:
        raise InspectionError("could not run systemctl") from exc
    if result.returncode != 0:
        # systemctl's stderr can contain implementation details. Keep errors
        # concise and avoid accidentally echoing sensitive configuration.
        raise InspectionError(
            f"systemctl could not complete the read-only {arguments[0]} request "
            f"(exit status {result.returncode})"
        )
    return result.stdout


def _property_map(output: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            properties[name] = value
    return properties


def inspect_unit(name: str) -> Unit:
    raw = _systemctl(
        "show",
        "--no-pager",
        "--all",
        *[f"--property={property_name}" for property_name in _PROPERTIES],
        name,
    )
    properties = _property_map(raw)
    load_state = properties.get("LoadState", "")
    if load_state != "loaded":
        raise InspectionError(f"unit {name!r} is not loaded by the system manager")
    active_state = properties.get("ActiveState", "unknown")
    if active_state not in {"active", "reloading"}:
        raise InspectionError(f"unit {name!r} is not active (state: {active_state})")
    try:
        main_pid = int(properties.get("MainPID", "0"))
    except ValueError as exc:
        raise InspectionError("systemd returned an invalid MainPID") from exc
    if main_pid <= 0:
        raise InspectionError(f"unit {name!r} has no running main process")

    need_reload: bool | None
    reload_text = properties.get("NeedDaemonReload")
    if reload_text in {"yes", "no"}:
        need_reload = reload_text == "yes"
    else:
        need_reload = None

    return Unit(
        name=properties.get("Id", name),
        load_state=load_state,
        active_state=active_state,
        sub_state=properties.get("SubState", "unknown"),
        main_pid=main_pid,
        need_daemon_reload=need_reload,
        properties=properties,
    )


def cat_unit(name: str) -> str:
    return _systemctl("cat", "--no-pager", name)


def manager_environment_value(name: str) -> tuple[bool, str | None, bool]:
    """Inspect one manager variable; return presence, parsed value, and certainty."""
    output = _systemctl("show-environment")
    for line in output.splitlines():
        variable, separator, raw_value = line.partition("=")
        if separator and variable == name:
            # systemctl prints this block in shell-compatible form. Compare
            # only plain characters that need no shell unescaping.
            if _SAFE_MANAGER_VALUE.fullmatch(raw_value):
                return True, raw_value, True
            return True, None, False
    return False, None, True


def read_process_variable(pid: int, requested_name: str) -> bytes | None:
    path = Path(f"/proc/{pid}/environ")
    try:
        raw = path.read_bytes()
    except PermissionError as exc:
        raise InspectionError(
            f"permission denied reading {path}; run unitenv as a user allowed to inspect this process"
        ) from exc
    except FileNotFoundError as exc:
        raise InspectionError(f"process {pid} exited while it was being inspected") from exc
    except OSError as exc:
        raise InspectionError(f"could not read the environment snapshot for process {pid}") from exc

    requested = requested_name.encode("ascii")
    prefix = requested + b"="
    if raw.startswith(prefix):
        value_start = len(prefix)
    else:
        entry_start = raw.find(b"\0" + prefix)
        if entry_start < 0:
            return None
        value_start = entry_start + 1 + len(prefix)
    value_end = raw.find(b"\0", value_start)
    if value_end < 0:
        value_end = len(raw)
    return raw[value_start:value_end]


def process_start_time(pid: int) -> float | None:
    """Return an approximate Unix timestamp for a process, or None if unavailable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # The command name is parenthesized and may itself contain spaces or
        # parentheses; fields after its final ')' begin at field 3 (state).
        tail = stat[stat.rfind(")") + 1 :].split()
        start_ticks = int(tail[19])  # field 22, starttime
        boot_time_line = next(
            line for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
            if line.startswith("btime ")
        )
        boot_time = int(boot_time_line.split()[1])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        return boot_time + start_ticks / ticks_per_second
    except (OSError, ValueError, StopIteration, IndexError, ZeroDivisionError):
        return None


def valid_unit_name(name: str) -> bool:
    return (
        name.endswith(".service")
        and "/" not in name
        and "\0" not in name
        and bool(name[:-8])
        and not name.startswith("-")
        and not re.search(r"\s", name)
    )
