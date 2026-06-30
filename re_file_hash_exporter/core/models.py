from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


SuffixCounts = dict[str, Counter[int]]
PathCounts = dict[str, Counter[str]]


@dataclass(slots=True)
class DmpScanResult:
    dmp_files: list[Path] = field(default_factory=list)
    suffix_counts: SuffixCounts = field(default_factory=dict)
    unversioned_paths: PathCounts = field(default_factory=dict)
    versioned_paths: PathCounts = field(default_factory=dict)
    scanned_bytes: int = 0

    @property
    def detected_extension_count(self) -> int:
        return len(self.suffix_counts)

    @property
    def unversioned_extension_count(self) -> int:
        return len(self.unversioned_paths)

    @property
    def unversioned_unique_path_count(self) -> int:
        return sum(len(paths) for paths in self.unversioned_paths.values())

    @property
    def unversioned_occurrence_count(self) -> int:
        return sum(sum(paths.values()) for paths in self.unversioned_paths.values())

    @property
    def versioned_unique_path_count(self) -> int:
        return sum(len(paths) for paths in self.versioned_paths.values())

    def merge(self, other: "DmpScanResult") -> None:
        self.dmp_files.extend(other.dmp_files)
        self.scanned_bytes += other.scanned_bytes
        for ext, versions in other.suffix_counts.items():
            self.suffix_counts.setdefault(ext, Counter()).update(versions)
        for ext, paths in other.unversioned_paths.items():
            self.unversioned_paths.setdefault(ext, Counter()).update(paths)
        for ext, paths in other.versioned_paths.items():
            self.versioned_paths.setdefault(ext, Counter()).update(paths)


@dataclass(slots=True)
class BruteForceOptions:
    selected_extensions: list[str]
    min_version: int = 0
    max_version: int = 4096
    mode: str = "small_range"
    custom_versions: str = ""
    neighbor_radius: int = 32
    date_start: str = ""
    date_end: str = ""
    processes: int = 0
    include_platform_suffixes: bool = True
    include_languages: bool = True
    include_streaming: bool = True
    request_gpu: bool = False
    gpu_batch_size: int = 16384
    include_versioned_extensions: bool = False


@dataclass(slots=True)
class BruteForceMatch:
    extension: str
    version: int
    raw_path: str
    full_path: str
    source: str = "pak-hash"


@dataclass(slots=True)
class BruteForceResult:
    matches: list[BruteForceMatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False

    def versions_by_extension(self) -> SuffixCounts:
        out: SuffixCounts = {}
        for match in self.matches:
            out.setdefault(match.extension, Counter())[match.version] += 1
        return out
