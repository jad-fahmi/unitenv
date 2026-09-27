"""Conservative source discovery from systemctl output and current files."""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass
from pathlib import Path


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_OPTIONAL_PATH = re.compile(r"\s+\(ignore_errors=(?:yes|no)\)")


def _directive_words(value: str) -> tuple[list[str], bool]:
    words, exact = systemd_words_with_status(value)
    return (words if words else [""]), exact


@dataclass(frozen=True)
class SourceRef:
    path: str
    optional: bool = False
    declared_in: str | None = None
    declared_line: int | None = None
    resolved: bool = False


@dataclass(frozen=True)
class Assignment:
    name: str
    kind: str
    source: str
    line: int | None
    modified_since_start: bool | None = None
    value: str | None = None
    value_known: bool = False


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
    words, _exact = systemd_words_with_status(value)
    return words


def systemd_words_with_status(value: str) -> tuple[list[str], bool]:
    """Split systemd words and report whether all syntax was understood."""
    words: list[str] = []
    current: list[str] = []
    quote: str | None = None
    started = False
    quote_just_closed = False
    exact = True
    index = 0
    escapes = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "s": " ",
        "\\": "\\",
        '"': '"',
        "'": "'",
    }
    while index < len(value):
        char = value[index]
        if char == "\\":
            if index + 1 >= len(value):
                current.append("\\")
                exact = False
                started = True
                index += 1
                continue
            next_char = value[index + 1]
            digits: str | None = None
            digit_count = 0
            base = 16
            escaped_prefix_length = 1
            if next_char == "x":
                digit_count = 2
                escaped_prefix_length = 2
                digits = value[index + 2 : index + 2 + digit_count]
            elif next_char == "u":
                digit_count = 4
                escaped_prefix_length = 2
                digits = value[index + 2 : index + 2 + digit_count]
            elif next_char == "U":
                digit_count = 8
                escaped_prefix_length = 2
                digits = value[index + 2 : index + 2 + digit_count]
            elif next_char in "01234567" and index + 3 < len(value):
                base = 8
                digit_count = 3
                digits = value[index + 1 : index + 1 + digit_count]
            if digits is not None and len(digits) == digit_count:
                try:
                    codepoint = int(digits, base)
                    if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                        raise ValueError
                    if (next_char == "x" or base == 8) and codepoint > 0x7F:
                        # Byte escapes above ASCII cannot be compared safely
                        # with Unicode decoded from procfs.
                        exact = False
                    current.append(chr(codepoint))
                    index += escaped_prefix_length + digit_count
                    started = True
                    quote_just_closed = False
                    continue
                except ValueError:
                    exact = False
            elif next_char in escapes:
                current.append(escapes[next_char])
                index += 2
                started = True
                quote_just_closed = False
                continue
            else:
                exact = False
            current.extend(("\\", next_char))
            started = True
            index += 2
            quote_just_closed = False
            continue
        if quote is not None:
            if char == quote:
                quote = None
                quote_just_closed = True
            else:
                current.append(char)
            started = True
            index += 1
            continue
        if char in {"'", '"'}:
            if started:
                exact = False
            quote = char
            started = True
        elif char.isspace():
            if started:
                words.append("".join(current))
                current.clear()
                started = False
            quote_just_closed = False
        else:
            if quote_just_closed:
                exact = False
            current.append(char)
            started = True
            quote_just_closed = False
        index += 1
    if started:
        words.append("".join(current))
    if quote is not None:
        exact = False
    return words, exact


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
        if pending is not None and raw_line.lstrip().startswith(("#", ";")):
            continue
        stripped = raw_line.rstrip()
        trailing_backslashes = len(stripped) - len(stripped.rstrip("\\"))
        continuation = trailing_backslashes % 2 == 1
        fragment = stripped[:-1] if continuation else raw_line
        if pending is None:
            pending = fragment
            pending_line = source_line
        else:
            pending += " " + fragment
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
            assignments, syntax_exact = _directive_words(value)
            if not syntax_exact:
                warnings.append("some Environment= values could not be parsed exactly")
            for assignment in assignments:
                if assignment == "":
                    environment.clear()
                    continue
                name, separator, assigned_value = assignment.partition("=")
                if separator and _NAME.fullmatch(name):
                    environment.append(
                        Assignment(
                            name,
                            "Environment=",
                            location,
                            line_number,
                            value=assigned_value,
                            value_known=(
                                syntax_exact
                                and "%" not in assigned_value
                                and all(char.isprintable() for char in assigned_value)
                            ),
                        )
                    )
        elif key == "EnvironmentFile":
            paths, syntax_exact = _directive_words(value)
            if not syntax_exact:
                warnings.append("some EnvironmentFile paths could not be parsed exactly")
                continue
            for path in paths:
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
            names, syntax_exact = _directive_words(value)
            if not syntax_exact:
                warnings.append("some PassEnvironment names could not be parsed exactly")
            for name in names:
                if name == "":
                    pass_environment.clear()
                elif _NAME.fullmatch(name):
                    pass_environment.add(name)
        elif key == "UnsetEnvironment":
            items, syntax_exact = _directive_words(value)
            if not syntax_exact:
                warnings.append("some UnsetEnvironment entries could not be parsed exactly")
            for item in items:
                if item == "":
                    unset_environment.clear()
                else:
                    unset_environment.append(item)

    return ParsedUnit(environment, environment_files, pass_environment, unset_environment, warnings)


def parse_systemd_environment_property(value: str, kind: str) -> list[Assignment]:
    assignments: list[Assignment] = []
    items, syntax_exact = systemd_words_with_status(value)
    for item in items:
        name, separator, assigned_value = item.partition("=")
        if separator and _NAME.fullmatch(name):
            assignments.append(
                Assignment(
                    name,
                    kind,
                    "loaded systemd unit property",
                    None,
                    value=assigned_value,
                    value_known=(
                        syntax_exact
                        and "%" not in assigned_value
                        and all(char.isprintable() for char in assigned_value)
                    ),
                )
            )
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


def _parse_environment_file_value(
    lines: list[str], start: int, right_hand_side: str
) -> tuple[str | None, int, bool]:
    """Parse one systemd EnvironmentFile value, returning its next line index."""
    leading_trimmed = right_hand_side.lstrip(" \t\r")
    quote: str | None = None
    if leading_trimmed.startswith(("'", '"')):
        quote = leading_trimmed[0]
        pieces: list[str] = []
        text = leading_trimmed[1:]
        line_index = start
        while True:
            index = 0
            continuation = False
            while index < len(text):
                char = text[index]
                if quote == "'":
                    if char == "'":
                        remainder = text[index + 1 :]
                        if remainder.strip(" \t\r"):
                            return None, line_index + 1, False
                        return "".join(pieces), line_index + 1, True
                    pieces.append(char)
                    index += 1
                    continue

                if char == '"':
                    remainder = text[index + 1 :]
                    if remainder.strip(" \t\r"):
                        return None, line_index + 1, False
                    return "".join(pieces), line_index + 1, True
                if char == "\\":
                    if index + 1 < len(text):
                        following = text[index + 1]
                        if following in {'"', "\\", "`", "$"}:
                            pieces.append(following)
                        else:
                            pieces.extend(("\\", following))
                        index += 2
                        continue
                    continuation = True
                    break
                pieces.append(char)
                index += 1

            if line_index + 1 >= len(lines):
                return None, line_index + 1, False
            if not continuation:
                pieces.append("\n")
            line_index += 1
            text = lines[line_index]

    # In an unquoted value, only a backslash at the very end continues the
    # physical line. Quotes after the first non-whitespace character are data.
    pieces = []
    line_index = start
    text = leading_trimmed
    while True:
        continuation = False
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\\":
                if index + 1 < len(text):
                    pieces.append(text[index + 1])
                    index += 2
                    continue
                continuation = True
                break
            pieces.append(char)
            index += 1
        if not continuation:
            break
        if line_index + 1 >= len(lines):
            return None, line_index + 1, False
        line_index += 1
        text = lines[line_index]
    return "".join(pieces).strip(" \t\r"), line_index + 1, True


def environment_file_assignments(
    content: str, requested_name: str
) -> tuple[list[tuple[int, str | None]], bool]:
    """Return a name's current values and whether any value syntax was unclear."""
    lines = content.split("\n")
    matches: list[tuple[int, str | None]] = []
    had_unparsed_value = False
    line_index = 0
    while line_index < len(lines):
        line = lines[line_index]
        stripped = line.lstrip(" \t\r")
        if not stripped or stripped.startswith(("#", ";")):
            line_index += 1
            continue
        assignment = _ASSIGNMENT.match(stripped)
        if assignment is None:
            line_index += 1
            continue

        name = assignment.group(1)
        equals_index = assignment.end() - 1
        value, next_line, parsed = _parse_environment_file_value(
            lines, line_index, stripped[equals_index + 1 :]
        )
        if not parsed:
            had_unparsed_value = True
        if name == requested_name:
            matches.append((line_index + 1, value if parsed else None))
        line_index = max(line_index + 1, next_line)
    return matches, had_unparsed_value


def parse_environment_files_property(value: str) -> list[SourceRef] | None:
    """Parse systemctl's `path (ignore_errors=...)` rendering when available."""
    if not value:
        return []
    refs: list[SourceRef] = []
    start = 0
    found = False
    for match in _OPTIONAL_PATH.finditer(value):
        segment = value[start : match.start()].strip()
        words, syntax_exact = systemd_words_with_status(segment)
        if not syntax_exact:
            return None
        if words:
            if len(words) != 1:
                return None
            path = words[0]
            refs.append(
                SourceRef(path, "yes" in match.group(0), "loaded unit property", None, True)
            )
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
        paths = _expand_path(source)
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
            if "\0" in content or "\ufeff" in content:
                warnings.append(f"EnvironmentFile contains invalid NUL or BOM characters: {path}")
                continue

            changed_since_start: bool | None = None
            try:
                if process_started is not None:
                    changed_since_start = path.stat().st_mtime > process_started
            except OSError:
                pass

            file_assignments, had_unparsed_value = environment_file_assignments(content, name)
            if had_unparsed_value:
                warnings.append(f"some EnvironmentFile values could not be parsed exactly: {path}")
            for line_number, assigned_value in file_assignments:
                if assigned_value is not None:
                    candidates.append(
                        Assignment(
                            name,
                            "EnvironmentFile=",
                            str(path),
                            line_number,
                            changed_since_start,
                            assigned_value,
                            True,
                        )
                    )
                else:
                    candidates.append(
                        Assignment(
                            name,
                            "EnvironmentFile=",
                            str(path),
                            line_number,
                            changed_since_start,
                        )
                    )

    unset_mentions_name = _unset_mentions_name(name, parsed.unset_environment)
    return candidates, warnings, unset_mentions_name


def _expand_path(source: SourceRef) -> list[str]:
    path = source.path
    if not path.startswith("/"):
        return []
    if source.resolved:
        return [path]
    if "%" in path:
        return []
    if glob.has_magic(path):
        return sorted(glob.glob(path))
    return [path]


def unset_property_mentions_name(name: str, value: str) -> bool:
    return _unset_mentions_name(name, parse_unset_property(value))
