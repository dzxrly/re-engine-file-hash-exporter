from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..constants import DEFAULT_PLATFORM_SUFFIXES, DEFAULT_PREFIXES, IGNORED_RESOURCE_EXTENSIONS
from ..models import DmpScanResult
from ..dmp.parser import normalize_path, raw_path_from_reference, strip_prefix_ignore_case


@dataclass(frozen=True, slots=True)
class RawPathEntry:
    raw_path: str
    seen_plain: bool = False
    seen_streaming: bool = False
    platform_suffixes: tuple[str, ...] = ()


def collect_raw_path_entries_by_extension(
    scan: DmpScanResult,
    selected: Iterable[str],
    include_versioned_extensions: bool = False,
) -> dict[str, list[RawPathEntry]]:
    selected_set = {ext.lower() for ext in selected if ext.lower() not in IGNORED_RESOURCE_EXTENSIONS}
    out: dict[str, dict[str, _RawPathFlags]] = {}

    def add_paths(extension: str, paths: Iterable[str]) -> None:
        normalized_extension = extension.lower()
        if normalized_extension in IGNORED_RESOURCE_EXTENSIONS or normalized_extension not in selected_set:
            return
        by_raw = out.setdefault(normalized_extension, {})
        for path in paths:
            raw_path = raw_path_from_reference(path)
            flags = by_raw.setdefault(raw_path, _RawPathFlags())
            flags.add(path)

    for extension, paths in scan.unversioned_paths.items():
        add_paths(extension, paths)

    if include_versioned_extensions:
        for extension, paths in scan.versioned_paths.items():
            add_paths(extension, paths)

    return {
        extension: [
            RawPathEntry(
                raw_path=raw_path,
                seen_plain=flags.seen_plain,
                seen_streaming=flags.seen_streaming,
                platform_suffixes=tuple(sorted(flags.platform_suffixes)),
            )
            for raw_path, flags in sorted(by_raw.items())
        ]
        for extension, by_raw in sorted(out.items())
    }


class _RawPathFlags:
    __slots__ = ("seen_plain", "seen_streaming", "platform_suffixes")

    def __init__(self) -> None:
        self.seen_plain = False
        self.seen_streaming = False
        self.platform_suffixes: set[str] = set()

    def add(self, reference_path: str) -> None:
        if _path_has_streaming_prefix(reference_path):
            self.seen_streaming = True
        else:
            self.seen_plain = True
        self.platform_suffixes.update(_platform_suffixes_from_path(reference_path))


def _path_after_default_prefix(path: str) -> str:
    normalized = normalize_path(path)
    for prefix in DEFAULT_PREFIXES:
        rest = strip_prefix_ignore_case(normalized, prefix)
        if rest is not None:
            return rest
    lower = normalized.lower()
    for prefix in DEFAULT_PREFIXES:
        pos = lower.find(prefix.lower())
        if pos >= 0:
            return normalized[pos + len(prefix) :]
    return normalized


def _path_has_streaming_prefix(path: str) -> bool:
    return strip_prefix_ignore_case(_path_after_default_prefix(path), "streaming/") is not None


def _platform_suffixes_from_path(path: str) -> tuple[str, ...]:
    known = {suffix.lower(): suffix for suffix in DEFAULT_PLATFORM_SUFFIXES}
    parts = normalize_path(path).rsplit("/", 1)[-1].split(".")
    suffixes: list[str] = []
    while parts:
        suffix = known.get(parts[-1].lower())
        if suffix is None:
            break
        suffixes.append(suffix)
        parts.pop()
    return tuple(reversed(suffixes))
