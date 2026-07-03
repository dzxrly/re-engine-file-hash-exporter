from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Callable, Iterable

from ..hash_utf16 import PreparedMixedText, prepare_mixed_text
from .candidate_policy import iter_candidate_bases
from .path_catalog import RawPathEntry

CancelCallback = Callable[[], bool]
VersionFoundCallback = Callable[[int], bool]


@dataclass(slots=True)
class PreparedGpuSuffix:
    text: str
    upper_units: tuple[int, ...]
    lower_units: tuple[int, ...]


@dataclass(slots=True)
class PreparedGpuBase:
    raw_path: str
    base_text: str
    base_upper_units: tuple[int, ...]
    base_lower_units: tuple[int, ...]
    suffixes: tuple[PreparedGpuSuffix, ...]

    def full_path(self, version: int, suffix_index: int) -> str:
        return f"{self.base_text}{version}{self.suffixes[suffix_index].text}"


@dataclass(slots=True)
class PreparedGpuBatch:
    upper_units: array
    lower_units: array
    offsets: array
    lengths: array
    base_indexes: array
    suffix_indexes: array
    versions: array

    @classmethod
    def empty(cls) -> "PreparedGpuBatch":
        return cls(
            upper_units=array("H"),
            lower_units=array("H"),
            offsets=array("I"),
            lengths=array("H"),
            base_indexes=array("I"),
            suffix_indexes=array("H"),
            versions=array("Q"),
        )

    def __len__(self) -> int:
        return len(self.versions)

    @property
    def min_version(self) -> int | None:
        if not self.versions:
            return None
        return min(int(version) for version in self.versions)

    @property
    def max_version(self) -> int | None:
        if not self.versions:
            return None
        return max(int(version) for version in self.versions)

    def append(
        self,
        base_index: int,
        suffix_index: int,
        version: int,
        upper_units: tuple[int, ...],
        lower_units: tuple[int, ...],
    ) -> None:
        length = len(upper_units)
        if length != len(lower_units):
            raise ValueError("Prepared GPU candidate upper/lower unit lengths differ.")
        if length > 0xFFFF:
            raise ValueError("Prepared GPU candidate is too long for the compact batch format.")

        self.offsets.append(len(self.upper_units))
        self.lengths.append(length)
        self.base_indexes.append(int(base_index))
        self.suffix_indexes.append(int(suffix_index))
        self.versions.append(int(version))
        self.upper_units.extend(upper_units)
        self.lower_units.extend(lower_units)

    def raw_path_at(self, index: int, bases: tuple[PreparedGpuBase, ...]) -> str:
        return bases[int(self.base_indexes[index])].raw_path

    def full_path_at(self, index: int, bases: tuple[PreparedGpuBase, ...]) -> str:
        return bases[int(self.base_indexes[index])].full_path(
            int(self.versions[index]),
            int(self.suffix_indexes[index]),
        )


def iter_prepared_gpu_batches(
    bases: tuple[PreparedGpuBase, ...],
    versions: list[int],
    batch_size: int,
    cancel_requested: CancelCallback | None,
    found_versions: set[int] | None = None,
    is_version_found: VersionFoundCallback | None = None,
):
    batch = PreparedGpuBatch.empty()
    discovered_versions = found_versions if found_versions is not None else set()

    def version_found(version: int) -> bool:
        return version in discovered_versions or bool(is_version_found and is_version_found(version))

    suffix_cache = _PreparedSuffixCache()
    for version in versions:
        if version_found(version):
            continue
        if cancel_requested and cancel_requested():
            break
        version_text = str(version)
        version_prepared = suffix_cache.prepare(version_text)
        for base_index, base in enumerate(bases):
            if version_found(version):
                break
            if cancel_requested and cancel_requested():
                break
            for suffix_index, upper_units, lower_units in _iter_gpu_candidates(base, version_prepared):
                if version_found(version):
                    break
                batch.append(base_index, suffix_index, version, upper_units, lower_units)
                if len(batch) >= batch_size:
                    yield batch
                    batch = PreparedGpuBatch.empty()
    if batch:
        yield batch


def prepare_gpu_bases(
    entries: Iterable[RawPathEntry],
    extension: str,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None,
) -> tuple[PreparedGpuBase, ...]:
    suffix_cache = _PreparedSuffixCache()
    return tuple(
        _prepare_gpu_base(base, suffix_cache)
        for entry in entries
        for base in iter_candidate_bases(
            entry,
            extension,
            include_platform_suffixes,
            language_mode,
            include_streaming,
            profiles,
        )
    )


def candidate_count_for_prepared_bases(bases: tuple[PreparedGpuBase, ...], version_count: int) -> int:
    if version_count <= 0:
        return 0
    return sum(len(base.suffixes) for base in bases) * int(version_count)


def _prepare_gpu_base(base, suffix_cache: "_PreparedSuffixCache") -> PreparedGpuBase:
    prepared = prepare_mixed_text(base.base_text)
    return PreparedGpuBase(
        raw_path=base.raw_path,
        base_text=base.base_text,
        base_upper_units=prepared.upper_units,
        base_lower_units=prepared.lower_units,
        suffixes=_prepare_suffixes(base.platform_suffixes, base.language_suffixes, suffix_cache),
    )


def _prepare_suffixes(
    platform_suffixes: tuple[str, ...],
    language_suffixes: tuple[str, ...],
    suffix_cache: "_PreparedSuffixCache",
) -> tuple[PreparedGpuSuffix, ...]:
    suffixes = [_make_suffix("")]
    for language in language_suffixes:
        suffixes.append(_make_suffix(f".{language}", suffix_cache.dotted(language)))
    for platform in platform_suffixes:
        platform_suffix_text = f".{platform}"
        platform_prepared = suffix_cache.dotted(platform)
        suffixes.append(_make_suffix(platform_suffix_text, platform_prepared))
        for language in language_suffixes:
            language_prepared = suffix_cache.dotted(language)
            suffixes.append(
                _make_suffix(
                    f"{platform_suffix_text}.{language}",
                    platform_prepared,
                    language_prepared,
                )
            )
    return tuple(suffixes)


def _make_suffix(text: str, *prepared_parts: PreparedMixedText) -> PreparedGpuSuffix:
    upper_units: tuple[int, ...] = ()
    lower_units: tuple[int, ...] = ()
    for part in prepared_parts:
        upper_units += part.upper_units
        lower_units += part.lower_units
    return PreparedGpuSuffix(text=text, upper_units=upper_units, lower_units=lower_units)


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
    base: PreparedGpuBase,
    version_prepared: PreparedMixedText,
):
    for suffix_index, suffix in enumerate(base.suffixes):
        yield (
            suffix_index,
            base.base_upper_units + version_prepared.upper_units + suffix.upper_units,
            base.base_lower_units + version_prepared.lower_units + suffix.lower_units,
        )
