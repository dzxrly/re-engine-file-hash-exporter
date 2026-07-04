from __future__ import annotations

import re

from ..constants import DEFAULT_PREFIXES, IGNORED_RESOURCE_EXTENSIONS, RESOURCE_PATH_PREFIXES, TAG_SUFFIXES

_VALID_EXTENSION_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def accept_char(byte: int) -> bool:
    if byte == 0x20:
        return True
    if byte < 0x21 or byte > 0x7E:
        return False
    return byte not in b'"*\\:<>?*|'


def validate_path(path: str) -> bool:
    if len(path) < 3 or "/" not in path:
        return False
    tail = path.rsplit("/", 1)[1]
    dot_pos = tail.find(".")
    return 0 < dot_pos < len(tail) - 1


def normalize_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("@") or normalized.startswith("/"):
        normalized = normalized[1:]
    return normalized


def valid_extension(extension: str) -> bool:
    return _VALID_EXTENSION_RE.fullmatch(extension) is not None


def is_ignored_extension(extension: str) -> bool:
    return extension.lower() in IGNORED_RESOURCE_EXTENSIONS


def strip_known_tail_tags(parts: list[str]) -> list[str]:
    parts = list(parts)
    while parts and parts[-1].lower() in TAG_SUFFIXES:
        parts.pop()
    return parts


def resource_suffix_from_path(path: str) -> tuple[tuple[str, int] | None, tuple[str, str] | None]:
    normalized = normalize_path(path)
    if not validate_path(normalized):
        return None, None

    tail = normalized.rsplit("/", 1)[1]
    parts = strip_known_tail_tags(tail.split("."))
    if len(parts) < 2:
        return None, None

    if len(parts) >= 3:
        version = parts[-1]
        extension = parts[-2]
        extension = extension.lower()
        if version.isdigit() and valid_extension(extension) and not is_ignored_extension(extension):
            return (extension, int(version)), None

    extension = parts[-1]
    extension = extension.lower()
    if valid_extension(extension) and not is_ignored_extension(extension):
        return None, (extension, normalized)

    return None, None


def versioned_raw_path_from_path(path: str) -> tuple[str, int, str] | None:
    normalized = normalize_path(path)
    if not validate_path(normalized):
        return None

    parent, tail = normalized.rsplit("/", 1)
    parts = strip_known_tail_tags(tail.split("."))
    if len(parts) < 3:
        return None

    version = parts[-1]
    extension = parts[-2]
    extension = extension.lower()
    if not version.isdigit() or not valid_extension(extension) or is_ignored_extension(extension):
        return None

    raw_tail = ".".join(parts[:-1])
    return extension, int(version), f"{parent}/{raw_tail}"


def looks_like_game_resource_path(path: str) -> bool:
    lower = normalize_path(path).lower()
    return lower.startswith(RESOURCE_PATH_PREFIXES)


def strip_prefix_ignore_case(path: str, prefix: str) -> str | None:
    if path[: len(prefix)].lower() == prefix.lower():
        return path[len(prefix) :]
    return None


def raw_path_from_reference(path: str, prefixes: list[str] | None = None) -> str:
    prefixes = prefixes or DEFAULT_PREFIXES
    normalized = normalize_path(path)

    for prefix in prefixes:
        rest = strip_prefix_ignore_case(normalized, prefix)
        if rest is not None:
            normalized = rest
            break
    else:
        lower = normalized.lower()
        for prefix in prefixes:
            pos = lower.find(prefix.lower())
            if pos >= 0:
                normalized = normalized[pos + len(prefix) :]
                break

    streaming = strip_prefix_ignore_case(normalized, "streaming/")
    if streaming is not None:
        normalized = streaming

    return normalized


def extension_from_raw_path(path: str) -> str | None:
    tail = path.rsplit("/", 1)[-1]
    if "." not in tail:
        return None
    ext = tail.rsplit(".", 1)[1]
    ext = ext.lower()
    if not valid_extension(ext) or is_ignored_extension(ext):
        return None
    return ext


def extract_versioned_suffix(path: str) -> tuple[str, int] | None:
    suffix, _missing = resource_suffix_from_path(path)
    return suffix
