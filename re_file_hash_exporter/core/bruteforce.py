from __future__ import annotations

import os
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from itertools import islice
from multiprocessing import Manager
from pathlib import Path
from typing import Callable, Iterable

from .candidates import (
    candidate_count,
    iter_candidate_parts,
    join_candidate_parts,
    normalize_language_mode,
)
from .constants import IGNORED_RESOURCE_EXTENSIONS
from .gpu_torch import match_extension_with_torch, torch_cuda_status
from .hash_utf16 import hash_mixed_parts
from .models import BruteForceMatch, BruteForceOptions, BruteForceProgress, BruteForceResult, DmpScanResult, SuffixCounts
from .pak_hash import load_hashes_from_paks
from .path_parser import raw_path_from_reference
from .version_profiles import describe_auto_profile, load_version_profiles, plan_auto_detect_versions

ProgressCallback = Callable[[object], None]
CancelCallback = Callable[[], bool]

_GLOBAL_HASHES: set[int] = set()
_GLOBAL_STOP = None
_GLOBAL_VERSIONS: list[int] = []
_GLOBAL_PROFILES: dict[str, dict] = {}


_CANCEL_CHECK_INTERVAL = 8192


def parse_custom_versions(text: str, cancel_requested: CancelCallback | None = None) -> list[int]:
    values: set[int] = set()
    for item in text.replace("\n", ",").split(","):
        _raise_if_cancelled(cancel_requested)
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text.strip())
            end = int(end_text.strip())
            if end < start:
                start, end = end, start
            for index, value in enumerate(range(start, end + 1), start=1):
                if index % _CANCEL_CHECK_INTERVAL == 0:
                    _raise_if_cancelled(cancel_requested)
                values.add(value)
        else:
            values.add(int(item))
    return sorted(values)


def plan_versions_for_extension(
    extension: str,
    known_suffixes: SuffixCounts,
    options: BruteForceOptions,
    auto_profiles: dict | None = None,
    cancel_requested: CancelCallback | None = None,
) -> list[int]:
    _raise_if_cancelled(cancel_requested)
    if options.mode == "custom":
        return parse_custom_versions(options.custom_versions, cancel_requested)

    if options.mode == "auto_detect":
        profiles = auto_profiles if auto_profiles is not None else load_version_profiles()
        return plan_auto_detect_versions(extension, known_suffixes, options, profiles, cancel_requested)

    if options.mode == "adaptive":
        values: set[int] = set()
        for known in known_suffixes.get(extension, Counter()):
            _raise_if_cancelled(cancel_requested)
            start = max(0, known - options.neighbor_radius)
            end = known + options.neighbor_radius
            for version in range(start, end + 1):
                values.add(version)
        if values:
            return sorted(values)

    start = max(0, int(options.min_version))
    end = max(start, int(options.max_version))
    return _build_version_range(start, end, cancel_requested)


def _build_version_range(
    start: int,
    end: int,
    cancel_requested: CancelCallback | None = None,
) -> list[int]:
    versions: list[int] = []
    for index, version in enumerate(range(start, end + 1), start=1):
        if index % _CANCEL_CHECK_INTERVAL == 0:
            _raise_if_cancelled(cancel_requested)
        versions.append(version)
    _raise_if_cancelled(cancel_requested)
    return versions


def _raise_if_cancelled(cancel_requested: CancelCallback | None) -> None:
    if cancel_requested and cancel_requested():
        raise InterruptedError("Brute-force planning was cancelled.")


def collect_raw_paths_by_extension(
    scan: DmpScanResult,
    selected: Iterable[str],
    include_versioned_extensions: bool = False,
) -> dict[str, list[str]]:
    selected_set = {ext.lower() for ext in selected if ext.lower() not in IGNORED_RESOURCE_EXTENSIONS}
    out: dict[str, set[str]] = {}
    for extension, paths in scan.unversioned_paths.items():
        if extension.lower() in IGNORED_RESOURCE_EXTENSIONS:
            continue
        if extension.lower() not in selected_set:
            continue
        raw_paths = {raw_path_from_reference(path) for path in paths}
        out.setdefault(extension.lower(), set()).update(raw_paths)

    if include_versioned_extensions:
        for extension, paths in scan.versioned_paths.items():
            if extension.lower() in IGNORED_RESOURCE_EXTENSIONS:
                continue
            if extension.lower() not in selected_set:
                continue
            raw_paths = {raw_path_from_reference(path) for path in paths}
            out.setdefault(extension.lower(), set()).update(raw_paths)

    return {extension: sorted(raw_paths) for extension, raw_paths in out.items()}


def _init_worker(
    hashes: set[int],
    stop_signal=None,
    versions: list[int] | None = None,
    profiles: dict[str, dict] | None = None,
) -> None:
    global _GLOBAL_HASHES
    global _GLOBAL_STOP
    global _GLOBAL_VERSIONS
    global _GLOBAL_PROFILES
    _GLOBAL_HASHES = hashes
    _GLOBAL_STOP = stop_signal
    _GLOBAL_VERSIONS = versions or []
    _GLOBAL_PROFILES = profiles or {}


def _clear_worker_state() -> None:
    _init_worker(set(), None, [], {})


def _worker_stop_requested() -> bool:
    if _GLOBAL_STOP is None:
        return False
    if callable(_GLOBAL_STOP):
        return bool(_GLOBAL_STOP())
    return bool(_GLOBAL_STOP.is_set())


def _match_chunk(args) -> tuple[list[tuple[str, int, str]], int | None, int | None, int]:
    raw_paths, extension, include_platform, language_mode, include_streaming = args
    matches: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    current_min: int | None = None
    current_max: int | None = None
    scanned_candidates = 0
    for raw_path in raw_paths:
        if _worker_stop_requested():
            return matches, current_min, current_max, scanned_candidates
        for version in _GLOBAL_VERSIONS:
            current_min = version if current_min is None else min(current_min, version)
            current_max = version if current_max is None else max(current_max, version)
            if _worker_stop_requested():
                return matches, current_min, current_max, scanned_candidates
            for parts in iter_candidate_parts(
                raw_path,
                extension,
                version,
                include_platform,
                language_mode,
                include_streaming,
                _GLOBAL_PROFILES,
            ):
                if _worker_stop_requested():
                    return matches, current_min, current_max, scanned_candidates
                hash_value = hash_mixed_parts(parts)
                scanned_candidates += 1
                if hash_value in _GLOBAL_HASHES:
                    full_path = join_candidate_parts(parts)
                    if full_path in seen:
                        continue
                    seen.add(full_path)
                    matches.append((raw_path, version, full_path))
    return matches, current_min, current_max, scanned_candidates


def _chunks(items: list[str], chunk_size: int):
    iterator = iter(items)
    while True:
        chunk = list(islice(iterator, chunk_size))
        if not chunk:
            break
        yield chunk


def gpu_status_message(requested: bool) -> str | None:
    if not requested:
        return None
    ok, message = torch_cuda_status()
    if ok:
        return message
    return f"GPU requested but unavailable: {message} Falling back to CPU multiprocessing."


class _BruteForceProgressTracker:
    def __init__(
        self,
        progress: ProgressCallback | None,
        total_extensions: int,
        total_scan_count: int,
        phase: str = "searching",
        phase_detail: str = "",
    ) -> None:
        self.progress = progress
        self.total_extensions = total_extensions
        self.total_scan_count = total_scan_count
        self.completed_extensions = 0
        self.completed_scan_count = 0
        self.current_extension = ""
        self.phase = phase
        self.phase_detail = phase_detail
        self.started_at = time.monotonic()
        self.last_emit_at = 0.0

    def set_phase(
        self,
        phase: str,
        phase_detail: str = "",
        total_extensions: int | None = None,
        total_scan_count: int | None = None,
        completed_extensions: int = 0,
        completed_scan_count: int = 0,
    ) -> None:
        self.phase = phase
        self.phase_detail = phase_detail
        if total_extensions is not None:
            self.total_extensions = total_extensions
        if total_scan_count is not None:
            self.total_scan_count = total_scan_count
        self.completed_extensions = completed_extensions
        self.completed_scan_count = completed_scan_count
        self.current_extension = ""
        self.emit(force=True)

    def set_planning_progress(
        self,
        completed_extensions: int,
        planned_scan_count: int,
        current_extension: str,
        force: bool = False,
    ) -> None:
        self.phase = "planning"
        self.completed_extensions = min(self.total_extensions, completed_extensions)
        self.completed_scan_count = max(0, planned_scan_count)
        self.current_extension = current_extension
        self.phase_detail = f"Planning .{current_extension}" if current_extension else "Planning candidates"
        self.emit(force=force)

    def advance_scans(self, count: int, current_extension: str) -> None:
        if count <= 0:
            return
        self.phase = "searching"
        self.phase_detail = f"Searching .{current_extension}" if current_extension else "Searching"
        self.completed_scan_count = min(self.total_scan_count, self.completed_scan_count + count)
        self.current_extension = current_extension
        self.emit()

    def finish_extension(self, current_extension: str) -> None:
        self.phase = "searching"
        self.phase_detail = f"Finished .{current_extension}" if current_extension else "Searching"
        self.completed_extensions = min(self.total_extensions, self.completed_extensions + 1)
        self.current_extension = current_extension
        self.emit(force=True)

    def emit(self, force: bool = False) -> None:
        if not self.progress:
            return
        now = time.monotonic()
        is_complete = (
            self.completed_extensions >= self.total_extensions
            or self.completed_scan_count >= self.total_scan_count > 0
        )
        if not force and not is_complete and now - self.last_emit_at < 0.2:
            return
        self.last_emit_at = now
        self.progress(
            BruteForceProgress(
                completed_extensions=self.completed_extensions,
                total_extensions=self.total_extensions,
                completed_scan_count=self.completed_scan_count,
                total_scan_count=self.total_scan_count,
                elapsed_seconds=now - self.started_at,
                current_extension=self.current_extension,
                phase=self.phase,
                phase_detail=self.phase_detail,
            )
        )


def brute_force_suffixes(
    scan: DmpScanResult,
    pak_paths: list[Path],
    known_suffixes: SuffixCounts,
    options: BruteForceOptions,
    progress: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
) -> BruteForceResult:
    def stopped() -> bool:
        return bool(cancel_requested and cancel_requested())

    warnings: list[str] = []
    use_gpu = False
    gpu_message = gpu_status_message(options.request_gpu)
    if gpu_message:
        use_gpu = gpu_message.startswith("torch CUDA backend ready:")
        if not use_gpu:
            warnings.append(gpu_message)
        if progress:
            progress(gpu_message)

    raw_by_ext = collect_raw_paths_by_extension(
        scan,
        options.selected_extensions,
        include_versioned_extensions=options.include_versioned_extensions,
    )
    if not raw_by_ext:
        return BruteForceResult(warnings=["No raw paths found for selected extensions."])

    if stopped():
        return BruteForceResult(cancelled=True)

    tracker = _BruteForceProgressTracker(
        progress,
        total_extensions=len(raw_by_ext),
        total_scan_count=0,
        phase="loading_paks",
        phase_detail=f"Loading metadata from {len(pak_paths)} PAK file(s)",
    )
    tracker.emit(force=True)

    if progress:
        progress("Loading PAK entry hashes...")
    pak_hashes = load_hashes_from_paks(pak_paths, workers=min(8, len(pak_paths)), progress=progress)
    if stopped():
        if progress:
            progress("Stop requested. Brute-force search cancelled before matching.")
        tracker.set_phase("loading_paks", "Cancelled while loading PAK metadata")
        return BruteForceResult(cancelled=True)

    if progress:
        progress(f"Loaded {len(pak_hashes)} unique PAK hashes.")
        progress(
            "Planning candidate versions for: "
            + ", ".join(f".{extension}" for extension in sorted(raw_by_ext))
        )

    result = BruteForceResult(warnings=warnings)
    profiles = load_version_profiles()
    auto_profiles = profiles if options.mode == "auto_detect" else {}
    language_mode = normalize_language_mode(options.language_mode, options.include_languages)
    planned_versions: dict[str, list[int]] = {}
    candidate_counts_by_ext: dict[str, int] = {}
    planned_scan_count = 0
    planned_extension_count = 0
    tracker.set_phase(
        "planning",
        "Planning candidate versions",
        total_extensions=len(raw_by_ext),
        total_scan_count=0,
    )
    for extension, raw_paths in raw_by_ext.items():
        if stopped():
            result.cancelled = True
            if progress:
                progress("Stop requested. Brute-force planning cancelled.")
            tracker.set_phase(
                "planning",
                "Cancelled while planning candidates",
                total_extensions=len(raw_by_ext),
                total_scan_count=0,
                completed_extensions=planned_extension_count,
                completed_scan_count=planned_scan_count,
            )
            return result

        tracker.set_planning_progress(planned_extension_count, planned_scan_count, extension)
        if progress:
            progress(f"Planning .{extension} candidate versions...")
        try:
            versions = plan_versions_for_extension(
                extension,
                known_suffixes,
                options,
                auto_profiles,
                cancel_requested=stopped,
            )
        except InterruptedError:
            result.cancelled = True
            if progress:
                progress("Stop requested. Brute-force planning cancelled.")
            tracker.set_phase(
                "planning",
                "Cancelled while planning candidates",
                total_extensions=len(raw_by_ext),
                total_scan_count=0,
                completed_extensions=planned_extension_count,
                completed_scan_count=planned_scan_count,
            )
            return result
        planned_versions[extension] = versions
        candidate_counts_by_ext[extension] = (
            candidate_count(
                raw_paths,
                extension,
                len(versions),
                options.include_platform_suffixes,
                language_mode,
                options.include_streaming,
                profiles,
            )
            if versions
            else 0
        )
        planned_scan_count += candidate_counts_by_ext[extension]
        planned_extension_count += 1
        tracker.set_planning_progress(planned_extension_count, planned_scan_count, extension, force=True)

    tracker.set_phase(
        "searching",
        "Searching candidates",
        total_extensions=len(raw_by_ext),
        total_scan_count=sum(candidate_counts_by_ext.values()),
    )
    if progress:
        progress(
            f"Planned {tracker.total_scan_count} path candidate scan(s) across "
            f"{tracker.total_extensions} extension(s)."
        )
        progress(
            "Brute-force search started for: "
            + ", ".join(f".{extension}" for extension in sorted(raw_by_ext))
        )

    processes = options.processes if options.processes and options.processes > 0 else os.cpu_count() or 1
    processes = max(1, processes)

    for extension, raw_paths in raw_by_ext.items():
        if stopped():
            result.cancelled = True
            if progress:
                progress("Stop requested. Brute-force search cancelled.")
            tracker.emit(force=True)
            return result

        versions = planned_versions[extension]
        if not versions:
            result.warnings.append(f"No candidate versions planned for {extension}.")
            tracker.finish_extension(extension)
            continue
        if options.mode == "auto_detect" and progress:
            progress(f".{extension}: auto_detect using {describe_auto_profile(extension, auto_profiles)}.")

        if use_gpu:
            if progress:
                progress(
                    f"Brute matching .{extension}: {len(raw_paths)} raw paths x {len(versions)} versions using torch CUDA."
                )
                progress(f".{extension}: version candidates {_format_candidate_range(versions)} ({len(versions)} total).")
            try:
                gpu_matches, cancelled = match_extension_with_torch(
                    extension=extension,
                    raw_paths=raw_paths,
                    versions=versions,
                    pak_hashes=pak_hashes,
                    include_platform_suffixes=options.include_platform_suffixes,
                    language_mode=language_mode,
                    include_streaming=options.include_streaming,
                    profiles=profiles,
                    progress=progress,
                    cancel_requested=cancel_requested,
                    scan_progress=lambda count, current_extension=extension: tracker.advance_scans(
                        count,
                        current_extension,
                    ),
                    batch_size=options.gpu_batch_size,
                )
            except RuntimeError as err:
                use_gpu = False
                result.warnings.append(f"GPU backend failed for .{extension}: {err}. Falling back to CPU.")
                if progress:
                    progress(f"GPU backend failed for .{extension}: {err}. Falling back to CPU.")
            else:
                for raw_path, version, full_path in gpu_matches:
                    result.matches.append(
                        BruteForceMatch(
                            extension=extension,
                            version=version,
                            raw_path=raw_path,
                            full_path=full_path,
                        )
                    )
                if cancelled:
                    result.cancelled = True
                    if progress:
                        progress("Brute-force search stopped by user.")
                    tracker.emit(force=True)
                    return result
                tracker.finish_extension(extension)
                continue

        if progress:
            total_candidates = candidate_counts_by_ext[extension]
            progress(
                f"Brute matching .{extension}: {len(raw_paths)} raw paths x {len(versions)} versions "
                f"({total_candidates} path candidates) using {processes} processes."
            )
            progress(f".{extension}: version candidates {_format_candidate_range(versions)} ({len(versions)} total).")

        chunk_size = max(1, min(64, len(raw_paths) // (processes * 2) + 1))
        tasks = [
            (
                chunk,
                extension,
                options.include_platform_suffixes,
                language_mode,
                options.include_streaming,
            )
            for chunk in _chunks(raw_paths, chunk_size)
        ]

        completed = 0
        if processes == 1:
            _init_worker(pak_hashes, stopped, versions, profiles)
            try:
                for task in tasks:
                    if stopped():
                        result.cancelled = True
                        if progress:
                            progress("Stop requested. Brute-force search cancelled.")
                        tracker.emit(force=True)
                        return result
                    completed += 1
                    chunk_matches, min_version, max_version, scanned_candidates = _match_chunk(task)
                    tracker.advance_scans(scanned_candidates, extension)
                    _record_chunk_matches(
                        result,
                        extension,
                        completed,
                        len(tasks),
                        chunk_matches,
                        progress,
                        min_version,
                        max_version,
                    )
                    if stopped():
                        result.cancelled = True
                        if progress:
                            progress("Stop requested. Brute-force search cancelled.")
                        tracker.emit(force=True)
                        return result
            finally:
                _clear_worker_state()
            tracker.finish_extension(extension)
            continue

        manager = Manager()
        stop_signal = manager.Event()
        executor = ProcessPoolExecutor(
            max_workers=processes,
            initializer=_init_worker,
            initargs=(pak_hashes, stop_signal, versions, profiles),
        )
        futures = [executor.submit(_match_chunk, task) for task in tasks]
        pending = set(futures)
        try:
            while pending:
                if stopped():
                    result.cancelled = True
                    stop_signal.set()
                    for future in pending:
                        future.cancel()
                    if progress:
                        progress("Stop requested. Cancelling pending brute-force work...")

                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    if future.cancelled():
                        continue
                    completed += 1
                    chunk_matches, min_version, max_version, scanned_candidates = future.result()
                    tracker.advance_scans(scanned_candidates, extension)
                    _record_chunk_matches(
                        result,
                        extension,
                        completed,
                        len(tasks),
                        chunk_matches,
                        progress,
                        min_version,
                        max_version,
                    )

                if result.cancelled and not pending:
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            manager.shutdown()

        if result.cancelled:
            if progress:
                progress("Brute-force search stopped by user.")
            tracker.emit(force=True)
            return result

        tracker.finish_extension(extension)

    return result


def _record_chunk_matches(
    result: BruteForceResult,
    extension: str,
    completed: int,
    total: int,
    chunk_matches: list[tuple[str, int, str]],
    progress: ProgressCallback | None,
    min_version: int | None,
    max_version: int | None,
) -> None:
    if progress:
        version_text = _format_version_progress(min_version, max_version)
        if chunk_matches:
            progress(f".{extension}: chunk {completed}/{total} {version_text} found {len(chunk_matches)} match(es).")
        else:
            progress(f".{extension}: completed chunk {completed}/{total} {version_text}.")

    for raw_path, version, full_path in chunk_matches:
        result.matches.append(
            BruteForceMatch(
                extension=extension,
                version=version,
                raw_path=raw_path,
                full_path=full_path,
            )
        )
        if progress:
            progress(f"MATCH .{extension}.{version} -> {full_path}")


def _format_version_progress(min_version: int | None, max_version: int | None) -> str:
    if min_version is None or max_version is None:
        return "versions unknown"
    if min_version == max_version:
        return f"version {min_version}"
    return f"versions {min_version}..{max_version}"


def _format_candidate_range(versions: list[int]) -> str:
    if not versions:
        return "none"
    low = min(versions)
    high = max(versions)
    if low == high:
        return str(low)
    return f"{low}..{high}"
