from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from ..hash_utf16 import PreparedMixedText, prepare_mixed_text
from .candidate_policy import iter_candidate_bases
from .path_catalog import RawPathEntry

CancelCallback = Callable[[], bool]


@dataclass(slots=True)
class PreparedGpuCandidate:
    raw_path: str
    version: int
    base_text: str
    version_text: str
    suffix_text: str
    upper_units: tuple[int, ...]
    lower_units: tuple[int, ...]

    @property
    def full_path(self) -> str:
        return f"{self.base_text}{self.version_text}{self.suffix_text}"


def iter_prepared_gpu_batches(
    entries: Iterable[RawPathEntry],
    extension: str,
    versions: list[int],
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None,
    batch_size: int,
    cancel_requested: CancelCallback | None,
):
    batch: list[PreparedGpuCandidate] = []
    bases = [
        _PreparedGpuBase.from_base(base)
        for entry in entries
        for base in iter_candidate_bases(
            entry,
            extension,
            include_platform_suffixes,
            language_mode,
            include_streaming,
            profiles,
        )
    ]
    suffix_cache = _PreparedSuffixCache()
    for version in versions:
        if cancel_requested and cancel_requested():
            break
        version_text = str(version)
        version_prepared = suffix_cache.prepare(version_text)
        for base in bases:
            if cancel_requested and cancel_requested():
                break
            for candidate in _iter_gpu_candidates(base, version, version_text, version_prepared, suffix_cache):
                batch.append(candidate)
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
    if batch:
        yield batch


@dataclass(slots=True)
class _PreparedGpuBase:
    raw_path: str
    base_text: str
    base_upper_units: tuple[int, ...]
    base_lower_units: tuple[int, ...]
    platform_suffixes: tuple[str, ...]
    language_suffixes: tuple[str, ...]

    @classmethod
    def from_base(cls, base) -> "_PreparedGpuBase":
        prepared = prepare_mixed_text(base.base_text)
        return cls(
            raw_path=base.raw_path,
            base_text=base.base_text,
            base_upper_units=prepared.upper_units,
            base_lower_units=prepared.lower_units,
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


def _iter_gpu_candidates(
    base: _PreparedGpuBase,
    version: int,
    version_text: str,
    version_prepared: PreparedMixedText,
    suffix_cache: _PreparedSuffixCache,
):
    yield _make_gpu_candidate(base, version, version_text, "", version_prepared)
    for language in base.language_suffixes:
        suffix_text = f".{language}"
        yield _make_gpu_candidate(
            base,
            version,
            version_text,
            suffix_text,
            version_prepared,
            suffix_cache.dotted(language),
        )
    for platform in base.platform_suffixes:
        platform_suffix_text = f".{platform}"
        platform_prepared = suffix_cache.dotted(platform)
        yield _make_gpu_candidate(
            base,
            version,
            version_text,
            platform_suffix_text,
            version_prepared,
            platform_prepared,
        )
        for language in base.language_suffixes:
            yield _make_gpu_candidate(
                base,
                version,
                version_text,
                f"{platform_suffix_text}.{language}",
                version_prepared,
                platform_prepared,
                suffix_cache.dotted(language),
            )


def _make_gpu_candidate(
    base: _PreparedGpuBase,
    version: int,
    version_text: str,
    suffix_text: str,
    version_prepared: PreparedMixedText,
    *suffixes: PreparedMixedText,
) -> PreparedGpuCandidate:
    upper_units = base.base_upper_units + version_prepared.upper_units
    lower_units = base.base_lower_units + version_prepared.lower_units
    for suffix in suffixes:
        upper_units += suffix.upper_units
        lower_units += suffix.lower_units
    return PreparedGpuCandidate(
        raw_path=base.raw_path,
        version=version,
        base_text=base.base_text,
        version_text=version_text,
        suffix_text=suffix_text,
        upper_units=upper_units,
        lower_units=lower_units,
    )

