"""Join one systemd unit's loaded settings, current files, and main process."""

from __future__ import annotations

from typing import Any

from .sources import (
    Assignment,
    ParsedUnit,
    SourceRef,
    find_assignments,
    parse_environment_files_property,
    parse_name_list_property,
    parse_systemd_environment_property,
    parse_unit_cat,
    unset_property_mentions_name,
)
from .systemd import (
    InspectionError,
    Unit,
    cat_unit,
    inspect_unit,
    manager_environment_has,
    process_start_time,
    read_process_variable,
)


def _configured_sources(
    name: str,
    parsed: ParsedUnit,
    unit: Unit,
    process_started: float | None,
) -> tuple[list[Assignment], list[str], bool, bool, bool | None]:
    properties = unit.properties
    loaded_environment = parse_systemd_environment_property(
        properties.get("Environment", ""), "Environment="
    )

    loaded_files: list[SourceRef] | None = None
    if "EnvironmentFiles" in properties:
        loaded_files = parse_environment_files_property(properties["EnvironmentFiles"])
    if loaded_files is None:
        environment_files = parsed.environment_files
    else:
        environment_files = loaded_files

    if "PassEnvironment" in properties:
        pass_names = parse_name_list_property(properties["PassEnvironment"])
    else:
        pass_names = parsed.pass_environment

    unset_property_available = "UnsetEnvironment" in properties
    if unset_property_available:
        unset_match = unset_property_mentions_name(name, properties["UnsetEnvironment"])
    else:
        unset_match = unset_property_mentions_name(name, " ".join(parsed.unset_environment))

    candidates, warnings, disk_unset_match = find_assignments(
        name, parsed, loaded_environment, environment_files, process_started
    )
    return (
        candidates,
        warnings,
        name in pass_names,
        unset_match or disk_unset_match,
        unit.need_daemon_reload,
    )


def explain(unit_name: str, variable: str, reveal: bool = False) -> tuple[dict[str, Any], int]:
    unit = inspect_unit(unit_name)
    warnings: list[str] = []
    try:
        parsed = parse_unit_cat(cat_unit(unit_name))
    except InspectionError:
        parsed = ParsedUnit([], [], set(), [], [])
        warnings.append("systemctl cat was unavailable; on-disk source attribution is incomplete")

    process_started = process_start_time(unit.main_pid)
    process_value = read_process_variable(unit.main_pid, variable)
    present = process_value is not None

    candidates, source_warnings, passed, unset, need_reload = _configured_sources(
        variable, parsed, unit, process_started
    )
    warnings.extend(source_warnings)

    if need_reload is True:
        warnings.append("systemd reports NeedDaemonReload=yes; current unit files differ from its loaded view")
    elif need_reload is None:
        warnings.append("systemd did not expose NeedDaemonReload for this unit")
    if process_started is None:
        warnings.append("could not determine the main process start time for file timestamp comparisons")

    manager_has_variable: bool | None = None
    if passed:
        try:
            manager_has_variable = manager_environment_has(variable)
        except InspectionError:
            warnings.append("could not inspect the system manager environment for the requested name")

    pam_name = unit.properties.get("PAMName", "")
    source_rows = []
    seen: set[tuple[str, str, int | None]] = set()
    for candidate in candidates:
        identity = (candidate.kind, candidate.source, candidate.line)
        if identity in seen:
            continue
        seen.add(identity)
        row: dict[str, Any] = {
            "kind": candidate.kind,
            "source": candidate.source,
            "line": candidate.line,
        }
        if candidate.modified_since_start is not None:
            row["modified_since_process_start"] = candidate.modified_since_start
        source_rows.append(row)

    if present:
        assessment = (
            "present in the main process snapshot; listed assignments are candidates, "
            "not proven historical sources"
        )
    elif candidates or passed:
        assessment = (
            "absent from the main process snapshot despite a current assignment or "
            "PassEnvironment rule; "
            "loaded and launch-time state may differ"
        )
    else:
        assessment = "absent from the main process snapshot; no matching current assignment was found"

    if unset:
        warnings.append(
            "UnsetEnvironment mentions this name; its final filter may remove a matching assignment"
        )
    if pam_name:
        warnings.append("PAMName is configured; PAM may add environment variables during service startup")
    if not source_rows and not passed:
        warnings.append(
            "other manager-generated or startup sources may exist; no unique provenance is inferred"
        )

    result: dict[str, Any] = {
        "unit": unit.name,
        "state": f"{unit.active_state}/{unit.sub_state}",
        "main_pid": unit.main_pid,
        "variable": variable,
        "process_snapshot": {"present": present},
        "loaded_configuration": {
            "need_daemon_reload": need_reload,
            "pass_environment": passed,
            "unset_environment_mentions_name": unset,
            "pam_name_configured": bool(pam_name),
        },
        "current_assignments": source_rows,
        "manager_environment": {
            "passed_by_pass_environment": passed,
            "name_present": manager_has_variable,
        },
        "assessment": assessment,
        "warnings": warnings,
    }
    if present:
        if reveal:
            result["process_snapshot"]["value"] = process_value.decode(
                "utf-8", errors="surrogateescape"
            )
        else:
            result["process_snapshot"]["value_redacted"] = True

    return result, 0 if present else 3
