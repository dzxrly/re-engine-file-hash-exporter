from __future__ import annotations

from typing import Iterable

from .search.candidate_policy import (
    candidate_count_for_entries,
    iter_candidate_bases,
    normalize_language_mode,
    should_search_languages,
)
from .search.path_catalog import RawPathEntry

CandidateParts = tuple[str, ...]


def candidate_count(
    raw_paths: Iterable[str],
    extension: str,
    version_count: int,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None = None,
) -> int:
    entries = [_compat_entry(raw_path) for raw_path in raw_paths]
    return candidate_count_for_entries(
        entries,
        extension,
        version_count,
        include_platform_suffixes,
        language_mode,
        include_streaming,
        profiles,
    )


def iter_candidate_parts(
    raw_path: str,
    extension: str,
    version: int,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None = None,
):
    version_text = str(version)
    for base in iter_candidate_bases(
        _compat_entry(raw_path),
        extension,
        include_platform_suffixes,
        language_mode,
        include_streaming,
        profiles,
    ):
        base_parts: CandidateParts = (base.base_text, version_text)
        yield base_parts
        for language in base.language_suffixes:
            yield (*base_parts, ".", language)
        for suffix in base.platform_suffixes:
            platform_parts: CandidateParts = (*base_parts, ".", suffix)
            yield platform_parts
            for language in base.language_suffixes:
                yield (*platform_parts, ".", language)


def join_candidate_parts(parts: CandidateParts) -> str:
    return "".join(parts)


def _compat_entry(raw_path: str) -> RawPathEntry:
    return RawPathEntry(raw_path=raw_path, seen_plain=True, seen_streaming=True)
