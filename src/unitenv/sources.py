"""Conservative source discovery from systemctl output and current files."""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass
from pathlib import Path


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_OPTIONAL_PATH = re.compile(r"\s+\(ignore_errors=(?:yes|no)\)")


def _directive_words(value: str) -> list[str]:
    words = systemd_words(value)
    return words if words else [""]


@dataclass(frozen=True)
class SourceRef:
    path: str
    optional: bool = False
    declared_in: str | None = None
    declared_line: int | None = None


@dataclass(frozen=True)
class Assignment:
    name: str
    kind: str
    source: str
    line: int | None
    modified_since_start: bool | None = None


@dataclass
class ParsedUnit:
    environment: list[Assignment]
    environment_files: list[SourceRef]
    pass_environment: set[str]
    unset_environment: list[str]
    warnings: list[str]


def systemd_words(value: str) -> list[str]:
    """Split common systemd syntax words, honoring quotes and C-like escapes.

    This intentionally handles only what source discovery needs. It does not
    claim to emulate all systemd specifier or escape processing.
    """
    words: list[str] = []
    current: list[str] = []
    quote: str | None = None
    started = False
    index = 0
    escapes = {"s": " ", "t": "\t", "n": "\n", "r": "\r", "\\": "\\", '"': '"', "'": "'"}
    while index < len(value):
        char = value[index]
        if char == "\\":
            if index + 1 >= len(value):
                current.append("\\")
                started = True
                index += 1
                continue
            next_char = value[index + 1]
            if next_char == "x" and index + 3 < len(value):
                digits = value[index + 2 : index + 4]
                try:
                    current.append(chr(int(digits, 16)))
                    index += 4
                    started = True
                    continue
                except ValueError:
                    pass
            current.append(escapes.get(next_char, next_char))
            started = True
            index += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            started = True
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            started = True
        elif char.isspace():
            if started:
                words.append("".join(current))
                current.clear()
                started = False
        else:
            current.append(char)
            started = True
        index += 1
    if started:
        words.append("".join(current))
    return words


def _logical_lines(text: str):
    source: str | None = None
    source_line = 0
    pending: str | None = None
    pending_line: int | None = None
    for raw_line in text.splitlines():
        header = re.match(r"^# (/.+)$", raw_line)
        if header:
            if pending is not None:
                yield source, pending_line, pending
                pending = None
            source = header.group(1)
            source_line = 0
            continue
        source_line += 1
        stripped = raw_line.rstrip()
        trailing_backslashes = len(stripped) - len(stripped.rstrip("\\"))
        continuation = trailing_backslashes % 2 == 1
        fragment = stripped[:-1] if continuation else raw_line
        if pending is None:
            pending = fragment
            pending_line = source_line
        else:
            pending += fragment.lstrip()
        if not continuation:
            yield source, pending_line, pending
            pending = None
            pending_line = None
    if pending is not None:
        yield source, pending_line, pending


def parse_unit_cat(text: str) -> ParsedUnit:
    environment: list[Assignment] = []
    environment_files: list[SourceRef] = []
    pass_environment: set[str] = set()
    unset_environment: list[str] = []
    warnings: list[str] = []
    section = ""

    for source, line_number, line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section != "Service" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        location = source or "unit configuration"

        if key == "Environment":
            for assignment in _directive_words(value):
                if assignment == "":
                    environment.clear()
                    continue
                name, separator, _assigned_value = assignment.partition("=")
                if separator and _NAME.fullmatch(name):
                    environment.append(Assignment(name, "Environment=", location, line_number))
        elif key == "EnvironmentFile":
            for path in _directive_words(value):
                if path == "":
                    environment_files.clear()
                    continue
                optional = path.startswith("-")
                actual_path = path[1:] if optional else path
                if actual_path:
                    environment_files.append(
                        SourceRef(actual_path, optional, location, line_number)
                    )
        elif key == "PassEnvironment":
            for name in _directive_words(value):
                if name == "":
                    pass_environment.clear()
                elif _NAME.fullmatch(name):
                    pass_environment.add(name)
        elif key == "UnsetEnvironment":
            for item in _directive_words(value):
                if item == "":
                    unset_environment.clear()
                else:
                    unset_environment.append(item)

    return ParsedUnit(environment, environment_files, pass_environment, unset_environment, warnings)


def parse_systemd_environment_property(value: str, kind: str) -> list[Assignment]:
    assignments: list[Assignment] = []
    for item in systemd_words(value):
        name, separator, _assigned_value = item.partition("=")
        if separator and _NAME.fullmatch(name):
            assignments.append(Assignment(name, kind, "loaded systemd unit property", None))
    return assignments


def parse_name_list_property(value: str) -> set[str]:
    result: set[str] = set()
    for item in systemd_words(value):
        name = item.split("=", 1)[0]
        if _NAME.fullmatch(name):
            result.add(name)
    return result


def parse_unset_property(value: str) -> list[str]:
    return systemd_words(value)


def environment_file_names(content: str):
    """Yield assignment names while skipping quoted and escaped continuations."""
    quote: str | None = None
    continued = False
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.lstrip()
        if quote is None and not continued and stripped and not stripped.startswith(("#", ";")):
            assignment = _ASSIGNMENT.match(stripped)
            if assignment:
                yield assignment.group(1), line_number

        if quote is None and not continued and stripped.startswith(("#", ";")):
            continue

        escaped = False
        for char in line:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif quote is not None:
                if char == quote:
                    quote = None
            elif char in {"'", '"'}:
                quote = char

        trailing_backslashes = len(line) - len(line.rstrip("\\"))
        continued = trailing_backslashes % 2 == 1
        if continued:
            # The final backslash escapes the physical newline itself.
            escaped = False


def parse_environment_files_property(value: str) -> list[SourceRef] | None:
    """Parse systemctl's `path (ignore_errors=...)` rendering when available."""
    if not value:
        return []
    refs: list[SourceRef] = []
    start = 0
    found = False
    for match in _OPTIONAL_PATH.finditer(value):
        segment = value[start : match.start()].strip()
        words = systemd_words(segment)
        if words:
            path = words[-1]
            refs.append(SourceRef(path, "yes" in match.group(0), "loaded unit property", None))
            found = True
        start = match.end()
    if not found:
        # Older or differently formatted systemd releases may not expose this
        # property in the familiar form; the caller can fall back to `cat`.
        return None
    return refs


def _unset_mentions_name(name: str, unset_items: list[str]) -> bool:
    return any(item.partition("=")[0] == name for item in unset_items)


def find_assignments(
    name: str,
    parsed: ParsedUnit,
    loaded_environment: list[Assignment],
    environment_files: list[SourceRef],
    process_started: float | None,
) -> tuple[list[Assignment], list[str], bool]:
    warnings = list(parsed.warnings)
    candidates = [item for item in parsed.environment if item.name == name]
    candidates.extend(item for item in loaded_environment if item.name == name)

    for source in environment_files:
        paths = _expand_path(source.path)
        if not paths:
            if not source.optional:
                warnings.append(f"could not resolve EnvironmentFile path {source.path!r}")
            continue
        for file_path in paths:
            path = Path(file_path)
            try:
                content = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                if not source.optional:
                    warnings.append(f"EnvironmentFile is missing: {path}")
                continue
            except (PermissionError, OSError):
                warnings.append(f"could not read EnvironmentFile: {path}")
                continue
            except UnicodeError:
                warnings.append(f"EnvironmentFile is not valid UTF-8: {path}")
                continue

            changed_since_start: bool | None = None
            try:
                if process_started is not None:
                    changed_since_start = path.stat().st_mtime > process_started
            except OSError:
                pass

            for assignment_name, line_number in environment_file_names(content):
                if assignment_name == name:
                    candidates.append(
                        Assignment(name, "EnvironmentFile=", str(path), line_number, changed_since_start)
                    )

    unset_mentions_name = _unset_mentions_name(name, parsed.unset_environment)
    return candidates, warnings, unset_mentions_name


def _expand_path(path: str) -> list[str]:
    if not path.startswith("/") or any(token in path for token in ("%", "$")):
        return []
    if glob.has_magic(path):
        return sorted(glob.glob(path))
    return [path]


def unset_property_mentions_name(name: str, value: str) -> bool:
    return _unset_mentions_name(name, parse_unset_property(value))
