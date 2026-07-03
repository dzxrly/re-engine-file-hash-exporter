from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from multiprocessing import Manager
from typing import Callable, Iterable

from ..gpu_torch import match_extension_with_torch
from ..models import BruteForceMatch
from ..version_plan import VersionPlan
from .path_catalog import RawPathEntry
from .progress import BruteForceProgressTracker, ProgressCallback

CancelCallback = Callable[[], bool]

_WORKER_DEVICE_ID: int | None = None
_WORKER_BATCH_SIZE: int = 16384
_WORKER_EXTENSION: str = ""
_WORKER_ENTRIES: list[RawPathEntry] = []
_WORKER_GROUP_HASHES: set[int] = set()
_WORKER_INCLUDE_PLATFORM_SUFFIXES = True
_WORKER_LANGUAGE_MODE = "localized"
_WORKER_INCLUDE_STREAMING = True
_WORKER_PROFILES: dict[str, dict] = {}
_WORKER_SHARED_FOUND_VERSIONS = None
_WORKER_STOP_SIGNAL = None


@dataclass(slots=True)
class MultiGpuSearchOutcome:
    matches: list[BruteForceMatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    completed: bool = True


@dataclass(slots=True)
class _GpuTaskResult:
    device_id: int
    versions: tuple[int, ...]
    matches: list[tuple[str, int, str]]
    scanned_candidates: int
    logs: list[str]
    cancelled: bool = False


@dataclass(slots=True)
class _GpuWorkerSlot:
    device_id: int
    batch_size: int
    index: int
    executor: ProcessPoolExecutor
    failed: bool = False

    @property
    def label(self) -> str:
        return f"cuda:{self.device_id}#{self.index}"

    def shutdown(self, cancel_futures: bool = False) -> None:
        self.executor.shutdown(wait=True, cancel_futures=cancel_futures)


def search_extension_multi_gpu(
    extension: str,
    entries: list[RawPathEntry],
    plan: VersionPlan,
    group_hashes: set[int],
    device_ids: list[int],
    batch_sizes_by_device: dict[int, int],
    workers_per_device: int,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict],
    version_chunk_size: int,
    tracker: BruteForceProgressTracker,
    progress: ProgressCallback | None,
    stopped: CancelCallback,
    found_versions: set[int],
) -> MultiGpuSearchOutcome:
    worker_devices = _expand_worker_devices(device_ids, workers_per_device)
    if not worker_devices:
        return MultiGpuSearchOutcome(completed=False, warnings=["No CUDA devices were selected for multi-GPU search."])

    pending_chunks = deque(tuple(chunk) for chunk in plan.iter_chunks(version_chunk_size, stopped))
    if not pending_chunks:
        tracker.finish_extension(extension)
        return MultiGpuSearchOutcome()

    outcome = MultiGpuSearchOutcome()
    reported_versions = {int(version) for version in found_versions}
    manager = Manager()
    shared_found_versions = manager.dict({int(version): True for version in found_versions})
    stop_signal = manager.Event()
    workers = [
        _GpuWorkerSlot(
            device_id=device_id,
            batch_size=max(1, int(batch_sizes_by_device.get(device_id, 16384))),
            index=index,
            executor=ProcessPoolExecutor(
                max_workers=1,
                initializer=_init_gpu_worker,
                initargs=(
                    device_id,
                    max(1, int(batch_sizes_by_device.get(device_id, 16384))),
                    extension,
                    entries,
                    group_hashes,
                    include_platform_suffixes,
                    language_mode,
                    include_streaming,
                    profiles,
                    shared_found_versions,
                    stop_signal,
                ),
            ),
        )
        for index, device_id in enumerate(worker_devices)
    ]
    futures: dict[Future, tuple[_GpuWorkerSlot, tuple[int, ...]]] = {}

    def active_workers() -> list[_GpuWorkerSlot]:
        return [worker for worker in workers if not worker.failed]

    def submit_next(worker: _GpuWorkerSlot) -> None:
        if worker.failed or not pending_chunks:
            return
        versions = pending_chunks.popleft()
        future = worker.executor.submit(_run_gpu_task, versions)
        futures[future] = (worker, versions)

    try:
        for worker in workers:
            submit_next(worker)

        while futures:
            if stopped():
                stop_signal.set()
                for future in futures:
                    future.cancel()
                outcome.cancelled = True
                if progress:
                    progress("Stop requested. Cancelling pending multi-GPU work...")
                return outcome

            done, _pending = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                worker, versions = futures.pop(future)
                if future.cancelled():
                    continue
                try:
                    task_result = future.result()
                except Exception as err:
                    worker.failed = True
                    pending_chunks.appendleft(versions)
                    warning = f".{extension}: {worker.label} failed: {err}"
                    outcome.warnings.append(warning)
                    if progress:
                        progress(f"Warning: {warning}")
                    worker.shutdown(cancel_futures=True)
                    if not active_workers():
                        outcome.completed = False
                        return outcome
                    continue

                _emit_task_logs(task_result, progress)
                tracker.advance_scans(task_result.scanned_candidates, extension)
                _record_task_matches(
                    outcome.matches,
                    extension,
                    task_result,
                    reported_versions,
                    found_versions,
                    shared_found_versions,
                )
                if task_result.cancelled:
                    outcome.cancelled = True
                    return outcome

                submit_next(worker)

        tracker.finish_extension(extension)
        found_versions.update(int(version) for version in shared_found_versions.keys())
        return outcome
    finally:
        stop_signal.set()
        for worker in workers:
            if not worker.failed:
                worker.shutdown(cancel_futures=True)
        manager.shutdown()


def _init_gpu_worker(
    device_id: int,
    batch_size: int,
    extension: str,
    entries: list[RawPathEntry],
    group_hashes: set[int],
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict],
    shared_found_versions,
    stop_signal,
) -> None:
    global _WORKER_DEVICE_ID
    global _WORKER_BATCH_SIZE
    global _WORKER_EXTENSION
    global _WORKER_ENTRIES
    global _WORKER_GROUP_HASHES
    global _WORKER_INCLUDE_PLATFORM_SUFFIXES
    global _WORKER_LANGUAGE_MODE
    global _WORKER_INCLUDE_STREAMING
    global _WORKER_PROFILES
    global _WORKER_SHARED_FOUND_VERSIONS
    global _WORKER_STOP_SIGNAL
    _WORKER_DEVICE_ID = int(device_id)
    _WORKER_BATCH_SIZE = max(1, int(batch_size))
    _WORKER_EXTENSION = extension
    _WORKER_ENTRIES = entries
    _WORKER_GROUP_HASHES = group_hashes
    _WORKER_INCLUDE_PLATFORM_SUFFIXES = include_platform_suffixes
    _WORKER_LANGUAGE_MODE = language_mode
    _WORKER_INCLUDE_STREAMING = include_streaming
    _WORKER_PROFILES = profiles
    _WORKER_SHARED_FOUND_VERSIONS = shared_found_versions
    _WORKER_STOP_SIGNAL = stop_signal


def _run_gpu_task(
    versions: tuple[int, ...],
) -> _GpuTaskResult:
    if _WORKER_DEVICE_ID is None:
        raise RuntimeError("GPU worker was not initialized.")

    logs: list[str] = []
    scanned_candidates = 0

    def progress(message: object) -> None:
        logs.append(str(message))

    def scan_progress(count: int) -> None:
        nonlocal scanned_candidates
        scanned_candidates += count

    def stopped() -> bool:
        return bool(_WORKER_STOP_SIGNAL and _WORKER_STOP_SIGNAL.is_set())

    shared_found_cache = {int(version) for version in _WORKER_SHARED_FOUND_VERSIONS.keys()}
    found_check_count = 0

    def refresh_shared_found_cache() -> None:
        shared_found_cache.update(int(version) for version in _WORKER_SHARED_FOUND_VERSIONS.keys())

    def is_version_found(version: int) -> bool:
        nonlocal found_check_count
        found_check_count += 1
        if found_check_count % 1024 == 0:
            refresh_shared_found_cache()
        return int(version) in shared_found_cache

    def mark_version_found(version: int) -> None:
        version = int(version)
        shared_found_cache.add(version)
        _WORKER_SHARED_FOUND_VERSIONS[version] = True

    local_found_versions = set(shared_found_cache)
    matches, cancelled = match_extension_with_torch(
        extension=_WORKER_EXTENSION,
        entries=_WORKER_ENTRIES,
        versions=list(versions),
        pak_hashes=_WORKER_GROUP_HASHES,
        include_platform_suffixes=_WORKER_INCLUDE_PLATFORM_SUFFIXES,
        language_mode=_WORKER_LANGUAGE_MODE,
        include_streaming=_WORKER_INCLUDE_STREAMING,
        profiles=_WORKER_PROFILES,
        progress=progress,
        cancel_requested=stopped,
        scan_progress=scan_progress,
        batch_size=_WORKER_BATCH_SIZE,
        found_versions=local_found_versions,
        device=f"cuda:{_WORKER_DEVICE_ID}",
        is_version_found=is_version_found,
        mark_version_found=mark_version_found,
    )
    return _GpuTaskResult(
        device_id=_WORKER_DEVICE_ID,
        versions=versions,
        matches=matches,
        scanned_candidates=scanned_candidates,
        logs=logs,
        cancelled=cancelled,
    )


def _expand_worker_devices(device_ids: Iterable[int], workers_per_device: int) -> list[int]:
    workers = max(1, int(workers_per_device))
    out: list[int] = []
    for device_id in device_ids:
        out.extend([int(device_id)] * workers)
    return out


def _emit_task_logs(task_result: _GpuTaskResult, progress: ProgressCallback | None) -> None:
    if not progress:
        return
    for message in task_result.logs:
        progress(message)


def _record_task_matches(
    out: list[BruteForceMatch],
    extension: str,
    task_result: _GpuTaskResult,
    reported_versions: set[int],
    found_versions: set[int],
    shared_found_versions,
) -> None:
    for raw_path, version, full_path in task_result.matches:
        version = int(version)
        if version in reported_versions:
            continue
        reported_versions.add(version)
        found_versions.add(version)
        shared_found_versions[version] = True
        out.append(
            BruteForceMatch(
                extension=extension,
                version=version,
                raw_path=raw_path,
                full_path=full_path,
            )
        )
