"""Command-line interface for unitenv."""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
from typing import Sequence

from . import __version__
from .inspect import explain
from .systemd import InspectionError, valid_unit_name


_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unitenv",
        description="Explain one environment variable for a running systemd service.",
        epilog=(
            "Values are hidden by default. /proc reports the process's initial exec environment, "
            "and current configuration can differ from launch-time state."
        ),
    )
    parser.add_argument("--version", action="version", version=f"unitenv {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    explain_parser = subparsers.add_parser(
        "explain", help="inspect one variable in a running system service"
    )
    explain_parser.add_argument("unit", help="systemd service unit, for example api.service")
    explain_parser.add_argument("--key", required=True, metavar="NAME", help="environment variable name")
    explain_parser.add_argument("--json", action="store_true", help="write a machine-readable JSON report")
    explain_parser.add_argument(
        "--reveal",
        action="store_true",
        help="include the requested process value in output (sensitive)",
    )
    return parser


def _format_human(report: dict) -> str:
    process = report["process_snapshot"]
    lines = [
        f"Unit:             {report['unit']}",
        f"State:            {report['state']}",
        f"Main PID:         {report['main_pid']}",
        f"Variable:         {report['variable']}",
    ]
    if process["present"]:
        if process.get("value_redacted"):
            snapshot = "present (value hidden)"
        else:
            snapshot = "present (value shown below)"
        lines.append(f"Process snapshot: {snapshot}")
        if "value" in process:
            lines.append(f"Value:            {json.dumps(process['value'], ensure_ascii=True)}")
    else:
        lines.append("Process snapshot: absent")

    loaded = report["loaded_configuration"]
    reload_state = loaded["need_daemon_reload"]
    reload_text = "unknown" if reload_state is None else ("yes" if reload_state else "no")
    lines.append(f"Need daemon reload: {reload_text}")
    lines.append("Current assignments:")
    candidates = report["current_assignments"]
    if candidates:
        for candidate in candidates:
            location = candidate["source"]
            if candidate["line"] is not None:
                location += f":{candidate['line']}"
            details = candidate["kind"]
            if candidate.get("modified_since_process_start") is True:
                details += "; file newer than process start"
            elif candidate.get("modified_since_process_start") is False:
                details += "; file not newer than process start"
            lines.append(f"  - {location} ({details})")
    else:
        lines.append("  - none found in the current unit files or referenced environment files")

    manager = report["manager_environment"]
    if manager["passed_by_pass_environment"]:
        manager_status = "present" if manager["name_present"] else "absent or unavailable"
        lines.append(
            f"PassEnvironment:  requested name is listed; manager value is {manager_status}"
        )
    else:
        lines.append("PassEnvironment:  requested name is not listed")
    if loaded["unset_environment_mentions_name"]:
        lines.append("UnsetEnvironment:  mentions this name (may remove a matching assignment)")

    lines.append(f"Assessment:       {report['assessment']}")
    if report["warnings"]:
        lines.append("Notes:")
        lines.extend(f"  - {warning}" for warning in report["warnings"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if platform.system() != "Linux":
        print("unitenv requires Linux and systemd", file=sys.stderr)
        return 2
    if not valid_unit_name(args.unit):
        print("unit must be a systemd service name ending in .service", file=sys.stderr)
        return 2
    if not _VARIABLE_NAME.fullmatch(args.key):
        print("--key must be a valid environment variable name", file=sys.stderr)
        return 2

    try:
        report, exit_status = explain(args.unit, args.key, args.reveal)
    except InspectionError as exc:
        print(f"unitenv: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print(_format_human(report))
    return exit_status
