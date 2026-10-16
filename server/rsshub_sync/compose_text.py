"""compose text (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .config import _normalise_managed_env_reference, _validate_compose_service
from .constants import COOKIE_KEYS, DEFAULT_MANAGED_ENV_PATH, DEFAULT_SERVICE
from .errors import SyncError
from .validation import validate_env_secret


_COMPOSE_SECRET_KEYS = frozenset(set(COOKIE_KEYS.values()) | {"TWITTER_AUTH_TOKEN"})
_COMPOSE_KEY_RE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*:(?P<value>.*)$")


def _compose_code_line(raw: str) -> Optional[Tuple[str, int, str]]:
    """Return ``(line, indent, stripped)`` for a non-comment YAML line."""

    line = raw.rstrip("\r\n")
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    prefix = line[: len(line) - len(line.lstrip(" "))]
    if "\t" in prefix:
        raise SyncError("compose indentation uses tabs")
    return line, len(prefix), stripped


def _compose_mapping(line: str) -> Optional[Tuple[int, str, str]]:
    match = _COMPOSE_KEY_RE.match(line)
    if not match:
        return None
    return len(match.group("indent")), match.group("key"), match.group("value")


def _compose_scalar(value: str) -> str:
    value = value.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        end = value.rfind(quote)
        if end < 1 or (value[end + 1 :].strip() and not value[end + 1 :].lstrip().startswith("#")):
            raise SyncError("compose scalar quoting is ambiguous")
        value = value[1:end]
    elif " #" in value:
        # YAML treats a # preceded by whitespace as a comment delimiter.  A
        # cookie value containing a literal '#' without preceding whitespace
        # remains untouched.
        value = value.split(" #", 1)[0].rstrip()
    return value


def _compose_service_block(
    lines: Sequence[str], service_name: str = DEFAULT_SERVICE
) -> Tuple[int, int, int, int]:
    """Locate the unique configured service mapping block."""

    _validate_compose_service(service_name)

    services: Optional[Tuple[int, int]] = None
    for index, raw in enumerate(lines):
        item = _compose_code_line(raw)
        if item is None:
            continue
        line, indent, _ = item
        mapping = _compose_mapping(line)
        if mapping and indent == 0 and mapping[1] == "services":
            if services is not None:
                raise SyncError("compose has duplicate services mappings")
            services = (index, indent)
    if services is None:
        raise SyncError("compose services mapping was not found")
    services_index, services_indent = services
    services_end = len(lines)
    for index in range(services_index + 1, len(lines)):
        item = _compose_code_line(lines[index])
        if item is not None and item[1] <= services_indent:
            services_end = index
            break

    matches: list[Tuple[int, int]] = []
    for index in range(services_index + 1, services_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and mapping[1] == service_name and item[1] > services_indent:
            matches.append((index, item[1]))
    if len(matches) != 1:
        raise SyncError("configured service was not found uniquely in compose file")
    service_index, service_indent = matches[0]
    service_end = len(lines)
    for index in range(service_index + 1, services_end):
        item = _compose_code_line(lines[index])
        if item is not None and item[1] <= service_indent:
            service_end = index
            break
    return service_index, service_end, service_indent, services_end


def _infer_service_child_indent(
    lines: Sequence[str], service_index: int, service_end: int, service_indent: int
) -> int:
    """Infer the actual indentation of direct service child keys."""

    first: Optional[Tuple[int, int, str]] = None
    for index in range(service_index + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is not None:
            first = (index, item[1], item[0])
            break
    if first is None:
        raise SyncError("configured service has no child keys")
    _, child_indent, first_line = first
    if child_indent <= service_indent or _compose_mapping(first_line) is None:
        raise SyncError("configured service child indentation is ambiguous")
    for index in range(first[0] + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and service_indent < item[1] < child_indent:
            raise SyncError("configured service child indentation is inconsistent")
    return child_indent


def _compose_node_end(lines: Sequence[str], start: int, end: int, parent_indent: int) -> int:
    for index in range(start + 1, end):
        item = _compose_code_line(lines[index])
        if item is not None and item[1] <= parent_indent:
            return index
    return end


def _secret_assignment(line: str) -> Optional[Tuple[str, str]]:
    """Parse one direct ``environment`` mapping/list assignment."""

    mapping = _compose_mapping(line)
    if mapping and mapping[1] in _COMPOSE_SECRET_KEYS:
        return mapping[1], _compose_scalar(mapping[2])
    stripped = line.strip()
    # YAML permits quoted mapping keys.  Support only the three exact managed
    # names; do not turn this conservative editor into a general YAML parser.
    for key in _COMPOSE_SECRET_KEYS:
        for quote in ("'", '"'):
            prefix = f"{quote}{key}{quote}"
            if stripped.startswith(prefix):
                remainder = stripped[len(prefix) :].lstrip()
                if remainder.startswith(":"):
                    return key, _compose_scalar(remainder[1:])
    if stripped.startswith("-"):
        # Compose also accepts the complete KEY=value list scalar in quotes,
        # for example `- "ZHIHU_COOKIES=z_c0=..."`.
        raw_item = stripped[1:].strip()
        whole_item_quoted = raw_item.startswith(("'", '"'))
        item = _compose_scalar(raw_item)
        for key in _COMPOSE_SECRET_KEYS:
            if item.startswith(key + "="):
                value = item[len(key) + 1 :]
                return key, value if whole_item_quoted else _compose_scalar(value)
    return None


def _looks_like_unparsed_secret_assignment(line: str) -> bool:
    """Fail closed when a direct environment entry starts with a secret key."""

    candidate = line.strip()
    if candidate.startswith("-"):
        candidate = candidate[1:].strip()
    candidate = candidate.lstrip("'\"")
    for key in _COMPOSE_SECRET_KEYS:
        if candidate.startswith(key):
            suffix = candidate[len(key) : len(key) + 1]
            if not suffix or suffix in {"=", ":", " ", "\t", "'", '"'}:
                return True
    return False


def _extract_rsshub_environment(
    lines: Sequence[str],
    service_index: int,
    service_end: int,
    child_indent: int,
    managed_env_path: str = DEFAULT_MANAGED_ENV_PATH,
) -> Tuple[Dict[str, str], Dict[int, str]]:
    """Extract only secrets in the configured service's environment."""

    managed_reference = _normalise_managed_env_reference(managed_env_path)

    environment_nodes: list[Tuple[int, int, str]] = []
    for index in range(service_index + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and item[1] == child_indent and mapping[1] == "environment":
            environment_nodes.append((index, item[1], mapping[2]))
    if len(environment_nodes) > 1:
        raise SyncError("configured service has duplicate environment mappings")
    if not environment_nodes:
        return {}, {}
    environment_index, environment_indent, value = environment_nodes[0]
    # ``{}`` is the valid Compose representation we write when all entries in
    # the original mapping were migrated.  Treat it as an empty mapping so
    # validation can prove the final document has no inline secrets.
    if value.strip() == "{}":
        return {}, {}
    if value.strip() and not value.lstrip().startswith("#"):
        raise SyncError("configured service environment mapping is not a block")
    environment_end = _compose_node_end(lines, environment_index, service_end, environment_indent)
    code: list[Tuple[int, int, str]] = []
    for index in range(environment_index + 1, environment_end):
        item = _compose_code_line(lines[index])
        if item is not None:
            code.append((index, item[1], item[0]))
    if not code:
        return {}, {}
    entry_indent = min(item[1] for item in code)
    if entry_indent <= environment_indent or any(indent != entry_indent for _, indent, _ in code):
        raise SyncError("configured service environment indentation is ambiguous")
    extracted: Dict[str, str] = {}
    replacements: Dict[int, str] = {}
    for index, _, line in code:
        assignment = _secret_assignment(line)
        if assignment is None:
            if _looks_like_unparsed_secret_assignment(line):
                raise SyncError("configured service secret environment entry is ambiguous")
            if _compose_mapping(line) is None and not line.strip().startswith("-"):
                raise SyncError("configured service environment entry is ambiguous")
            continue
        key, value = assignment
        if key in extracted:
            raise SyncError("configured service environment contains duplicate secret")
        if "$" in value or "{" in value:
            raise SyncError("compose secret uses interpolation; migrate it manually")
        validate_env_secret(key, value)
        extracted[key] = value
        replacements[index] = (" " * entry_indent) + "# migrated to " + managed_reference[2:] + "\n"
    return extracted, replacements


def _collapse_migrated_environment(
    lines: list[str],
    service_index: int,
    service_end: int,
    child_indent: int,
    migrated_lines: Mapping[int, str],
) -> bool:
    """Make a secret-only environment block an explicit empty mapping.

    Leaving the original ``environment:`` key with only migration comments
    gives YAML a null value, which Docker Compose rejects for some versions.
    Collapse it to ``environment: {}`` only when every code line in that block
    was migrated; unrelated environment entries must remain untouched.
    """

    environment_nodes: list[Tuple[int, str]] = []
    for index in range(service_index + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and item[1] == child_indent and mapping[1] == "environment":
            environment_nodes.append((index, lines[index]))
    if not environment_nodes:
        return False
    if len(environment_nodes) > 1:
        raise SyncError("configured service has duplicate environment mappings")

    environment_index, environment_line = environment_nodes[0]
    environment_end = _compose_node_end(lines, environment_index, service_end, child_indent)
    code_indices = [
        index
        for index in range(environment_index + 1, environment_end)
        if _compose_code_line(lines[index]) is not None
    ]
    if not code_indices or any(index not in migrated_lines for index in code_indices):
        return False

    line_without_newline = environment_line.rstrip("\r\n")
    mapping = _compose_mapping(line_without_newline)
    if mapping is None:  # pragma: no cover - guarded by the scan above
        raise SyncError("rsshub environment mapping is ambiguous")
    newline = environment_line[len(line_without_newline) :] or "\n"
    inline_comment = mapping[2] if mapping[2].lstrip().startswith("#") else ""
    lines[environment_index] = (
        line_without_newline[: mapping[0]] + "environment: {}" + inline_comment + newline
    )
    return True


def _inspect_env_file(
    lines: Sequence[str],
    service_index: int,
    service_end: int,
    child_indent: int,
    managed_env_path: str = DEFAULT_MANAGED_ENV_PATH,
) -> Dict[str, Any]:
    """Inspect the configured service's managed raw env-file entry."""

    managed_reference = _normalise_managed_env_reference(managed_env_path)

    nodes: list[Tuple[int, str]] = []
    for index in range(service_index + 1, service_end):
        item = _compose_code_line(lines[index])
        if item is None:
            continue
        mapping = _compose_mapping(item[0])
        if mapping and item[1] == child_indent and mapping[1] == "env_file":
            nodes.append((index, mapping[2]))
    if len(nodes) > 1:
        raise SyncError("configured service has duplicate env_file mappings")
    if not nodes:
        return {"present": False, "managed": False, "append_at": service_index + 1, "seq_indent": child_indent + 2}
    env_index, value = nodes[0]
    if value.strip() and not value.lstrip().startswith("#"):
        raise SyncError("configured service env_file mapping is not a block")
    env_end = _compose_node_end(lines, env_index, service_end, child_indent)
    code: list[Tuple[int, int, str]] = []
    for index in range(env_index + 1, env_end):
        item = _compose_code_line(lines[index])
        if item is not None:
            code.append((index, item[1], item[0]))
    if not code:
        return {"present": True, "managed": False, "append_at": env_end, "seq_indent": child_indent + 2}
    seq_indent = code[0][1]
    if seq_indent <= child_indent or not code[0][2].strip().startswith("-"):
        raise SyncError("configured service env_file list indentation is ambiguous")
    item_starts: list[Tuple[int, int, str]] = []
    for index, indent, line in code:
        stripped = line.strip()
        if indent < seq_indent:
            raise SyncError("configured service env_file list indentation is inconsistent")
        if indent == seq_indent:
            if not stripped.startswith("-"):
                raise SyncError("configured service env_file contains a non-list entry")
            item_starts.append((index, indent, stripped[1:].strip()))
    if not item_starts:
        raise SyncError("configured service env_file list is invalid")
    target_items = 0
    short_target = False
    for position, (item_index, _, item_value) in enumerate(item_starts):
        next_index = item_starts[position + 1][0] if position + 1 < len(item_starts) else env_end
        fields: Dict[str, str] = {}
        if item_value:
            pseudo = " " * (seq_indent + 2) + item_value
            mapping = _compose_mapping(pseudo)
            if mapping is None:
                if _compose_scalar(item_value) == managed_reference:
                    short_target = True
                continue
            fields[mapping[1]] = _compose_scalar(mapping[2])
        field_indent: Optional[int] = None
        for index in range(item_index + 1, next_index):
            item = _compose_code_line(lines[index])
            if item is None:
                continue
            if item[1] <= seq_indent:
                raise SyncError("configured service env_file item indentation is inconsistent")
            mapping = _compose_mapping(item[0])
            if mapping is None:
                raise SyncError("configured service env_file item is ambiguous")
            if field_indent is None:
                field_indent = item[1]
            elif field_indent != item[1]:
                raise SyncError("configured service env_file field indentation is inconsistent")
            if mapping[1] in fields:
                raise SyncError("configured service env_file item contains duplicate fields")
            fields[mapping[1]] = _compose_scalar(mapping[2])
        if fields.get("path") == managed_reference:
            target_items += 1
            if fields.get("format") != "raw":
                raise SyncError("configured service env_file managed entry is not format raw")
    if short_target:
        raise SyncError("configured service env_file target lacks format raw")
    if target_items > 1:
        raise SyncError("configured service env_file has duplicate managed entries")
    return {"present": True, "managed": target_items == 1, "append_at": env_end, "seq_indent": seq_indent}


def _validate_migrated_compose_text(
    text: str,
    service_name: str = DEFAULT_SERVICE,
    managed_env_path: str = DEFAULT_MANAGED_ENV_PATH,
) -> None:
    """Prove Compose text has no inline secrets and raw env wiring."""

    lines = text.splitlines(keepends=True)
    managed_reference = _normalise_managed_env_reference(managed_env_path)
    service_index, service_end, service_indent, _ = _compose_service_block(lines, service_name)
    child_indent = _infer_service_child_indent(lines, service_index, service_end, service_indent)
    extracted, _ = _extract_rsshub_environment(lines, service_index, service_end, child_indent)
    if extracted:
        raise SyncError("configured service still contains inline secrets")
    if not _inspect_env_file(lines, service_index, service_end, child_indent, managed_reference)["managed"]:
        raise SyncError("configured service does not reference managed env file")
