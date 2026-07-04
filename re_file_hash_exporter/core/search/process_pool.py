from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from itertools import islice
from multiprocessing import Manager
from typing import Callable

from ..models import SuffixDiscoveryMatch
from ..versions.plan import VersionPlan
from .cpu_matcher import ChunkResult, clear_worker_state, init_worker, match_chunk, match_entries
from .path_catalog import RawPathEntry
from .progress import SuffixDiscoveryProgressTracker, ProgressCallback

CancelCallback = Callable[[], bool]


@dataclass(slots=True)
class CpuSearchOutcome:
    matches: list[SuffixDiscoveryMatch]
    cancelled: bool = False


class CpuSearchExecutor:
    def __init__(
        self,
        processes: int,
        pak_hashes: set[int],
        profiles: dict[str, dict],
        stopped: CancelCallback,
    ) -> None:
        self.processes = max(1, processes)
        self.pak_hashes = pak_hashes
        self.profiles = profiles
        self.stopped = stopped
        self.manager = None
        self.stop_signal = None
        self.executor: ProcessPoolExecutor | None = None
        if self.processes > 1:
            self.manager = Manager()
            self.stop_signal = self.manager.Event()
            self.executor = ProcessPoolExecutor(
                max_workers=self.processes,
                initializer=init_worker,
                initargs=(pak_hashes, self.stop_signal, profiles),
            )

    def shutdown(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
        if self.manager is not None:
            self.manager.shutdown()
            self.manager = None

    def search_extension(
        self,
        extension: str,
        entries: list[RawPathEntry],
        plan: VersionPlan,
        include_platform_suffixes: bool,
        language_mode: str,
        include_streaming: bool,
        version_chunk_size: int,
        tracker: SuffixDiscoveryProgressTracker,
        progress: ProgressCallback | None,
        found_versions: set[int] | None = None,
    ) -> CpuSearchOutcome:
        discovered_versions = found_versions if found_versions is not None else set()
        chunk_size = max(1, min(64, len(entries) // (self.processes * 2) + 1))
        entry_chunks = list(_chunks(entries, chunk_size))
        version_chunk_count = max(1, (plan.count + version_chunk_size - 1) // version_chunk_size)
        total_task_chunks = len(entry_chunks) * version_chunk_count

        if self.processes == 1:
            return self._search_single_process(
                extension,
                entry_chunks,
                plan,
                include_platform_suffixes,
                language_mode,
                include_streaming,
                version_chunk_size,
                total_task_chunks,
                tracker,
                progress,
                discovered_versions,
            )

        return self._search_pool(
            extension,
            entry_chunks,
            plan,
            include_platform_suffixes,
            language_mode,
            include_streaming,
            version_chunk_size,
            total_task_chunks,
            tracker,
            progress,
            discovered_versions,
        )

    def _search_single_process(
        self,
        extension: str,
        entry_chunks: list[list[RawPathEntry]],
        plan: VersionPlan,
        include_platform_suffixes: bool,
        language_mode: str,
        include_streaming: bool,
        version_chunk_size: int,
        total_task_chunks: int,
        tracker: SuffixDiscoveryProgressTracker,
        progress: ProgressCallback | None,
        found_versions: set[int],
    ) -> CpuSearchOutcome:
        init_worker(self.pak_hashes, self.stopped, self.profiles)
        matches: list[SuffixDiscoveryMatch] = []
        reported_versions: set[int] = set()
        completed = 0
        try:
            for raw_version_chunk in plan.iter_chunks(version_chunk_size, self.stopped):
                version_chunk = [version for version in raw_version_chunk if version not in found_versions]
                if not version_chunk:
                    continue
                for entry_chunk in entry_chunks:
                    if self.stopped():
                        if progress:
                            progress("Stop requested. Suffix discovery cancelled.")
                        tracker.emit(force=True)
                        return CpuSearchOutcome(matches, cancelled=True)
                    completed += 1
                    result = match_entries(
                        entries=entry_chunk,
                        extension=extension,
                        versions=version_chunk,
                        pak_hashes=self.pak_hashes,
                        include_platform=include_platform_suffixes,
                        language_mode=language_mode,
                        include_streaming=include_streaming,
                        profiles=self.profiles,
                        stop_requested=self.stopped,
                        found_versions=found_versions,
                    )
                    tracker.advance_scans(result.scanned_candidates, extension)
                    _record_chunk_matches(
                        matches,
                        extension,
                        completed,
                        total_task_chunks,
                        result,
                        progress,
                        reported_versions,
                    )
        except InterruptedError:
            if progress:
                progress("Stop requested. Suffix discovery cancelled.")
            tracker.emit(force=True)
            return CpuSearchOutcome(matches, cancelled=True)
        finally:
            clear_worker_state()

        tracker.finish_extension(extension)
        return CpuSearchOutcome(matches)

    def _search_pool(
        self,
        extension: str,
        entry_chunks: list[list[RawPathEntry]],
        plan: VersionPlan,
        include_platform_suffixes: bool,
        language_mode: str,
        include_streaming: bool,
        version_chunk_size: int,
        total_task_chunks: int,
        tracker: SuffixDiscoveryProgressTracker,
        progress: ProgressCallback | None,
        found_versions: set[int],
    ) -> CpuSearchOutcome:
        if self.executor is None:
            raise RuntimeError("CPU search pool was not initialized.")

        matches: list[SuffixDiscoveryMatch] = []
        reported_versions: set[int] = set()
        completed = 0
        shared_found_versions = (
            self.manager.dict({version: True for version in found_versions}) if self.manager is not None else None
        )
        try:
            for raw_version_chunk in plan.iter_chunks(version_chunk_size, self.stopped):
                _sync_found_versions(found_versions, shared_found_versions)
                version_chunk = [version for version in raw_version_chunk if version not in found_versions]
                if not version_chunk:
                    continue
                tasks = [
                    (
                        entry_chunk,
                        extension,
                        version_chunk,
                        include_platform_suffixes,
                        language_mode,
                        include_streaming,
                        shared_found_versions,
                    )
                    for entry_chunk in entry_chunks
                ]
                futures = [self.executor.submit(match_chunk, task) for task in tasks]
                pending = set(futures)
                while pending:
                    if self.stopped():
                        if self.stop_signal is not None:
                            self.stop_signal.set()
                        for future in pending:
                            future.cancel()
                        if progress:
                            progress("Stop requested. Cancelling pending suffix discovery work...")

                    done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        if future.cancelled():
                            continue
                        completed += 1
                        result = future.result()
                        _sync_found_versions(found_versions, shared_found_versions)
                        found_versions.update(version for _raw_path, version, _full_path in result.matches)
                        tracker.advance_scans(result.scanned_candidates, extension)
                        _record_chunk_matches(
                            matches,
                            extension,
                            completed,
                            total_task_chunks,
                            result,
                            progress,
                            reported_versions,
                        )

                    if self.stopped() and not pending:
                        break
                if self.stopped():
                    if progress:
                        progress("Suffix discovery stopped by user.")
                    tracker.emit(force=True)
                    return CpuSearchOutcome(matches, cancelled=True)
        except InterruptedError:
            if progress:
                progress("Stop requested. Suffix discovery cancelled.")
            tracker.emit(force=True)
            return CpuSearchOutcome(matches, cancelled=True)

        _sync_found_versions(found_versions, shared_found_versions)
        tracker.finish_extension(extension)
        return CpuSearchOutcome(matches)


def _record_chunk_matches(
    out: list[SuffixDiscoveryMatch],
    extension: str,
    completed: int,
    total: int,
    result: ChunkResult,
    progress: ProgressCallback | None,
    reported_versions: set[int],
) -> None:
    new_matches = [
        (raw_path, version, full_path)
        for raw_path, version, full_path in result.matches
        if version not in reported_versions
    ]
    if progress:
        version_text = _format_version_progress(result.min_version, result.max_version)
        if new_matches:
            progress(f".{extension}: chunk {completed}/{total} {version_text} found {len(new_matches)} match(es).")
        else:
            progress(f".{extension}: completed chunk {completed}/{total} {version_text}.")

    for raw_path, version, full_path in new_matches:
        reported_versions.add(version)
        out.append(
            SuffixDiscoveryMatch(
                extension=extension,
                version=version,
                raw_path=raw_path,
                full_path=full_path,
            )
        )
        if progress:
            progress(f"MATCH .{extension}.{version} -> {full_path}")


def _chunks(items: list[RawPathEntry], chunk_size: int):
    iterator = iter(items)
    while True:
        chunk = list(islice(iterator, chunk_size))
        if not chunk:
            break
        yield chunk


def _format_version_progress(min_version: int | None, max_version: int | None) -> str:
    if min_version is None or max_version is None:
        return "versions unknown"
    if min_version == max_version:
        return f"version {min_version}"
    return f"versions {min_version}..{max_version}"


def _sync_found_versions(found_versions: set[int], shared_found_versions) -> None:
    if shared_found_versions is None:
        return
    found_versions.update(int(version) for version in shared_found_versions.keys())
