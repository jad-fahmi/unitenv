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
    manager_environment_value,
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
        environment_files = []
        seen_paths: set[str] = set()
        for source in [*loaded_files, *parsed.environment_files]:
            if source.path not in seen_paths:
                environment_files.append(source)
                seen_paths.add(source.path)

    if "PassEnvironment" in properties:
        pass_names = parse_name_list_property(properties["PassEnvironment"])
        pass_names.update(parsed.pass_environment)
    else:
        pass_names = parsed.pass_environment

    unset_property_available = "UnsetEnvironment" in properties
    if unset_property_available:
        unset_match = unset_property_mentions_name(name, properties["UnsetEnvironment"])
        unset_match = unset_match or unset_property_mentions_name(
            name, " ".join(parsed.unset_environment)
        )
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
    manager_value: str | None = None
    manager_value_known = False
    if passed:
        try:
            manager_has_variable, manager_value, manager_value_known = manager_environment_value(variable)
        except InspectionError:
            warnings.append("could not inspect the system manager environment for the requested name")

    pam_name = unit.properties.get("PAMName", "")
    source_rows = []
    seen: set[tuple[str, str, int | None, str | None, bool]] = set()
    process_text = (
        process_value.decode("utf-8", errors="surrogateescape")
        if process_value is not None
        else None
    )
    for candidate in candidates:
        identity = (
            candidate.kind,
            candidate.source,
            candidate.line,
            candidate.value,
            candidate.value_known,
        )
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
        if process_text is None:
            row["comparison"] = "not compared; variable absent from process snapshot"
        elif not candidate.value_known or candidate.value is None:
            row["comparison"] = "not compared; source value could not be parsed exactly"
        elif candidate.value == process_text:
            row["comparison"] = "matches process snapshot"
        else:
            row["comparison"] = "differs from process snapshot"
        source_rows.append(row)

    manager_comparison: str | None = None
    if manager_has_variable is True:
        if process_text is None:
            manager_comparison = "not compared; variable absent from process snapshot"
        elif not manager_value_known or manager_value is None:
            manager_comparison = "not compared; manager value could not be parsed exactly"
        elif manager_value == process_text:
            manager_comparison = "matches process snapshot"
        else:
            manager_comparison = "differs from process snapshot"

    if present:
        matching = sum(
            row["comparison"] == "matches process snapshot" for row in source_rows
        )
        comparable = sum(
            row["comparison"] in {
                "matches process snapshot",
                "differs from process snapshot",
            }
            for row in source_rows
        )
        if matching:
            matched_sources = f"{matching} current assignment candidate(s)"
            if manager_comparison == "matches process snapshot":
                matched_sources += " and the system manager environment"
            assessment = (
                f"present; its value matches {matched_sources}, "
                "but a matching value does not prove a unique historical source"
            )
        elif manager_comparison == "matches process snapshot" and comparable:
            assessment = (
                "present; the value matches the manager environment passed by PassEnvironment, "
                "while comparable current unit assignments differ"
            )
        elif comparable:
            assessment = (
                "present; none of the comparable current assignment values match; "
                "precedence, launch-time changes, or another source may explain it"
            )
        elif manager_comparison == "matches process snapshot":
            assessment = (
                "present; its value matches the system manager environment passed by "
                "PassEnvironment, but a unique historical source is not proven"
            )
        elif source_rows:
            assessment = "present; current assignments were found, but their values could not be compared"
        else:
            assessment = "present in the main process snapshot; no current assignment candidate was found"
    elif candidates:
        assessment = (
            "absent from the main process snapshot despite a current assignment; "
            "loaded and launch-time state may differ"
        )
    elif passed and manager_has_variable is True:
        assessment = (
            "absent from the main process snapshot although PassEnvironment and the system "
            "manager contain the name; loaded or launch-time state may differ"
        )
    elif passed:
        assessment = (
            "absent from the main process snapshot; PassEnvironment lists the name, but "
            "the manager value is absent or unavailable"
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
            "comparison": manager_comparison,
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
