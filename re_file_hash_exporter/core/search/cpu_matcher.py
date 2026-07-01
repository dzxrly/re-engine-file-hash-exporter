from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..hash_utf16 import MixedHashState, PreparedMixedText, prepare_mixed_text
from .candidate_policy import CandidateBase, iter_candidate_bases
from .path_catalog import RawPathEntry

CancelCallback = Callable[[], bool]

_GLOBAL_HASHES: set[int] = set()
_GLOBAL_STOP = None
_GLOBAL_PROFILES: dict[str, dict] = {}
_CANCEL_CHECK_INTERVAL = 8192


@dataclass(slots=True)
class ChunkResult:
    matches: list[tuple[str, int, str]]
    min_version: int | None
    max_version: int | None
    scanned_candidates: int


def init_worker(
    hashes: set[int],
    stop_signal=None,
    profiles: dict[str, dict] | None = None,
) -> None:
    global _GLOBAL_HASHES
    global _GLOBAL_STOP
    global _GLOBAL_PROFILES
    _GLOBAL_HASHES = hashes
    _GLOBAL_STOP = stop_signal
    _GLOBAL_PROFILES = profiles or {}


def clear_worker_state() -> None:
    init_worker(set(), None, {})


def worker_stop_requested() -> bool:
    if _GLOBAL_STOP is None:
        return False
    if callable(_GLOBAL_STOP):
        return bool(_GLOBAL_STOP())
    return bool(_GLOBAL_STOP.is_set())


def match_chunk(args) -> ChunkResult:
    found_versions = None
    if len(args) == 7:
        (
            entries,
            extension,
            versions,
            include_platform,
            language_mode,
            include_streaming,
            found_versions,
        ) = args
    else:
        (
            entries,
            extension,
            versions,
            include_platform,
            language_mode,
            include_streaming,
        ) = args
    return match_entries(
        entries=entries,
        extension=extension,
        versions=versions,
        pak_hashes=_GLOBAL_HASHES,
        include_platform=include_platform,
        language_mode=language_mode,
        include_streaming=include_streaming,
        profiles=_GLOBAL_PROFILES,
        stop_requested=worker_stop_requested,
        found_versions=found_versions,
    )


def match_entries(
    entries: list[RawPathEntry],
    extension: str,
    versions: list[int],
    pak_hashes: set[int],
    include_platform: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict],
    stop_requested: CancelCallback | None = None,
    found_versions=None,
) -> ChunkResult:
    matches: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    discovered_versions = found_versions if found_versions is not None else set()
    current_min: int | None = None
    current_max: int | None = None
    scanned_candidates = 0

    bases = [
        _PreparedCandidateBase.from_base(base)
        for entry in entries
        for base in iter_candidate_bases(
            entry,
            extension,
            include_platform,
            language_mode,
            include_streaming,
            profiles,
        )
    ]
    suffix_cache = _PreparedSuffixCache()

    for version in versions:
        if _version_found(discovered_versions, version):
            continue
        current_min = version if current_min is None else min(current_min, version)
        current_max = version if current_max is None else max(current_max, version)
        version_text = str(version)
        version_prepared = suffix_cache.prepare(version_text)

        for base in bases:
            if _version_found(discovered_versions, version):
                break
            if stop_requested and stop_requested():
                return ChunkResult(matches, current_min, current_max, scanned_candidates)
            scanned_candidates, matched = _scan_base_version(
                base=base,
                version=version,
                version_text=version_text,
                version_prepared=version_prepared,
                suffix_cache=suffix_cache,
                pak_hashes=pak_hashes,
                matches=matches,
                seen=seen,
                scanned_candidates=scanned_candidates,
            )
            if matched:
                _mark_version_found(discovered_versions, version)
                break
            if scanned_candidates % _CANCEL_CHECK_INTERVAL == 0 and stop_requested and stop_requested():
                return ChunkResult(matches, current_min, current_max, scanned_candidates)

    return ChunkResult(matches, current_min, current_max, scanned_candidates)


@dataclass(slots=True)
class _PreparedCandidateBase:
    raw_path: str
    base_text: str
    state: MixedHashState
    platform_suffixes: tuple[str, ...]
    language_suffixes: tuple[str, ...]

    @classmethod
    def from_base(cls, base: CandidateBase) -> "_PreparedCandidateBase":
        state = MixedHashState()
        state.write_text(base.base_text)
        return cls(
            raw_path=base.raw_path,
            base_text=base.base_text,
            state=state,
            platform_suffixes=base.platform_suffixes,
            language_suffixes=base.language_suffixes,
        )


class _PreparedSuffixCache:
    def __init__(self) -> None:
        self._items: dict[str, PreparedMixedText] = {}

    def prepare(self, text: str) -> PreparedMixedText:
        item = self._items.get(text)
        if item is None:
            item = prepare_mixed_text(text)
            self._items[text] = item
        return item

    def dotted(self, suffix: str) -> PreparedMixedText:
        return self.prepare(f".{suffix}")


def _scan_base_version(
    base: _PreparedCandidateBase,
    version: int,
    version_text: str,
    version_prepared: PreparedMixedText,
    suffix_cache: _PreparedSuffixCache,
    pak_hashes: set[int],
    matches: list[tuple[str, int, str]],
    seen: set[str],
    scanned_candidates: int,
) -> tuple[int, bool]:
    matched = False
    version_state = base.state.clone()
    version_state.write_prepared(version_prepared)
    before_count = len(matches)
    scanned_candidates = _record_if_match(
        state=version_state,
        base_text=base.base_text,
        version_text=version_text,
        suffix_text="",
        raw_path=base.raw_path,
        version=version,
        pak_hashes=pak_hashes,
        matches=matches,
        seen=seen,
        scanned_candidates=scanned_candidates,
    )
    if len(matches) > before_count:
        return scanned_candidates, True

    for language in base.language_suffixes:
        language_state = version_state.clone()
        language_state.write_prepared(suffix_cache.dotted(language))
        before_count = len(matches)
        scanned_candidates = _record_if_match(
            state=language_state,
            base_text=base.base_text,
            version_text=version_text,
            suffix_text=f".{language}",
            raw_path=base.raw_path,
            version=version,
            pak_hashes=pak_hashes,
            matches=matches,
            seen=seen,
            scanned_candidates=scanned_candidates,
        )
        if len(matches) > before_count:
            return scanned_candidates, True

    for platform in base.platform_suffixes:
        platform_state = version_state.clone()
        platform_state.write_prepared(suffix_cache.dotted(platform))
        platform_suffix_text = f".{platform}"
        before_count = len(matches)
        scanned_candidates = _record_if_match(
            state=platform_state,
            base_text=base.base_text,
            version_text=version_text,
            suffix_text=platform_suffix_text,
            raw_path=base.raw_path,
            version=version,
            pak_hashes=pak_hashes,
            matches=matches,
            seen=seen,
            scanned_candidates=scanned_candidates,
        )
        if len(matches) > before_count:
            return scanned_candidates, True
        for language in base.language_suffixes:
            language_state = platform_state.clone()
            language_state.write_prepared(suffix_cache.dotted(language))
            before_count = len(matches)
            scanned_candidates = _record_if_match(
                state=language_state,
                base_text=base.base_text,
                version_text=version_text,
                suffix_text=f"{platform_suffix_text}.{language}",
                raw_path=base.raw_path,
                version=version,
                pak_hashes=pak_hashes,
                matches=matches,
                seen=seen,
                scanned_candidates=scanned_candidates,
            )
            if len(matches) > before_count:
                return scanned_candidates, True

    return scanned_candidates, matched


def _record_if_match(
    state: MixedHashState,
    base_text: str,
    version_text: str,
    suffix_text: str,
    raw_path: str,
    version: int,
    pak_hashes: set[int],
    matches: list[tuple[str, int, str]],
    seen: set[str],
    scanned_candidates: int,
) -> int:
    scanned_candidates += 1
    if state.digest() in pak_hashes:
        full_path = f"{base_text}{version_text}{suffix_text}"
        if full_path in seen:
            return scanned_candidates
        seen.add(full_path)
        matches.append((raw_path, version, full_path))
    return scanned_candidates


def _version_found(found_versions, version: int) -> bool:
    return found_versions is not None and int(version) in found_versions


def _mark_version_found(found_versions, version: int) -> None:
    if found_versions is None:
        return
    version = int(version)
    add = getattr(found_versions, "add", None)
    if add is not None:
        add(version)
        return
    found_versions[version] = True
