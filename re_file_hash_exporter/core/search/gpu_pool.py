from __future__ import annotations

import atexit
import multiprocessing as mp
from array import array
from bisect import bisect_left
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from typing import Callable, Iterable

from ..gpu.torch_backend import match_extension_with_torch
from ..models import SuffixDiscoveryMatch
from ..versions.plan import VersionPlan
from .candidate_policy import candidate_count_for_entries
from .gpu_batches import PreparedGpuBase, prepare_gpu_bases
from .path_catalog import RawPathEntry
from .progress import SuffixDiscoveryProgressTracker, ProgressCallback

CancelCallback = Callable[[], bool]

_WORKER_DEVICE_ID: int | None = None
_WORKER_BATCH_SIZE: int = 16384
_WORKER_PRODUCER_COUNT: int = 1
_WORKER_PREFETCH_BATCHES: int = 0
_WORKER_EXTENSION: str = ""
_WORKER_ENTRIES: list[RawPathEntry] = []
_WORKER_GROUP_HASHES = set()
_WORKER_HASH_VIEW: _SharedHashView | None = None
_WORKER_PREPARED_BASES: tuple[PreparedGpuBase, ...] = ()
_WORKER_INCLUDE_PLATFORM_SUFFIXES = True
_WORKER_LANGUAGE_MODE = "localized"
_WORKER_INCLUDE_STREAMING = True
_WORKER_PROFILES: dict[str, dict] = {}
_WORKER_SHARED_FOUND_VERSIONS = None
_WORKER_STOP_SIGNAL = None


@dataclass(slots=True)
class MultiGpuSearchOutcome:
    matches: list[SuffixDiscoveryMatch] = field(default_factory=list)
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


@dataclass(frozen=True, slots=True)
class _SharedHashDescriptor:
    name: str
    count: int


@dataclass(slots=True)
class _SharedHashData:
    memory: shared_memory.SharedMemory
    descriptor: _SharedHashDescriptor

    def close(self) -> None:
        self.memory.close()
        try:
            self.memory.unlink()
        except FileNotFoundError:
            pass


class _SharedHashView:
    def __init__(self, descriptor: _SharedHashDescriptor) -> None:
        self._memory = shared_memory.SharedMemory(name=descriptor.name)
        self._count = int(descriptor.count)
        self._values = self._memory.buf.cast("Q") if self._count else None

    def __contains__(self, value: object) -> bool:
        if self._values is None:
            return False
        try:
            target = int(value) & 0xFFFF_FFFF_FFFF_FFFF
        except (TypeError, ValueError):
            return False
        index = bisect_left(self._values, target, 0, self._count)
        return index < self._count and int(self._values[index]) == target

    def __iter__(self):
        if self._values is None:
            return iter(())
        return (int(self._values[index]) for index in range(self._count))

    def close(self) -> None:
        if self._values is not None:
            self._values.release()
            self._values = None
        self._memory.close()


def search_extension_multi_gpu(
    extension: str,
    entries: list[RawPathEntry],
    plan: VersionPlan,
    group_hashes: set[int],
    device_ids: list[int],
    batch_sizes_by_device: dict[int, int],
    producers_per_device: int,
    prefetch_batches_per_device: int,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict],
    version_chunk_size: int,
    tracker: SuffixDiscoveryProgressTracker,
    progress: ProgressCallback | None,
    stopped: CancelCallback,
    found_versions: set[int],
) -> MultiGpuSearchOutcome:
    worker_devices = _cuda_owner_devices(device_ids)
    if not worker_devices:
        return MultiGpuSearchOutcome(completed=False, warnings=["No CUDA devices were selected for multi-GPU search."])

    chunk_size = _dynamic_version_chunk_size(
        entries,
        extension,
        plan,
        batch_sizes_by_device,
        include_platform_suffixes,
        language_mode,
        include_streaming,
        profiles,
        version_chunk_size,
        prefetch_batches_per_device,
    )
    chunk_iterator = (tuple(chunk) for chunk in plan.iter_chunks(chunk_size, stopped))
    pending_chunks: deque[tuple[int, ...]] = deque()

    outcome = MultiGpuSearchOutcome()
    reported_versions = {int(version) for version in found_versions}
    process_context = _gpu_process_context()
    manager = process_context.Manager()
    shared_found_versions = manager.dict({int(version): True for version in found_versions})
    stop_signal = manager.Event()
    shared_hashes = _create_shared_hash_data(group_hashes)
    workers = [
        _GpuWorkerSlot(
            device_id=device_id,
            batch_size=max(1, int(batch_sizes_by_device.get(device_id, 16384))),
            index=index,
            executor=ProcessPoolExecutor(
                max_workers=1,
                mp_context=process_context,
                initializer=_init_gpu_worker,
                initargs=(
                    device_id,
                    max(1, int(batch_sizes_by_device.get(device_id, 16384))),
                    max(1, int(producers_per_device or 1)),
                    max(0, int(prefetch_batches_per_device or 0)),
                    extension,
                    entries,
                    shared_hashes.descriptor,
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
        if worker.failed:
            return
        versions = _next_version_chunk(pending_chunks, chunk_iterator)
        if not versions:
            return
        future = worker.executor.submit(_run_gpu_task, versions)
        futures[future] = (worker, versions)

    try:
        for worker in workers:
            submit_next(worker)
        if not futures:
            tracker.finish_extension(extension)
            return outcome

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
        shared_hashes.close()


def _gpu_process_context() -> mp.context.BaseContext:
    return mp.get_context("spawn")


def _init_gpu_worker(
    device_id: int,
    batch_size: int,
    producer_count: int,
    prefetch_batches: int,
    extension: str,
    entries: list[RawPathEntry],
    hash_descriptor: _SharedHashDescriptor,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict],
    shared_found_versions,
    stop_signal,
) -> None:
    global _WORKER_DEVICE_ID
    global _WORKER_BATCH_SIZE
    global _WORKER_PRODUCER_COUNT
    global _WORKER_PREFETCH_BATCHES
    global _WORKER_EXTENSION
    global _WORKER_ENTRIES
    global _WORKER_GROUP_HASHES
    global _WORKER_HASH_VIEW
    global _WORKER_PREPARED_BASES
    global _WORKER_INCLUDE_PLATFORM_SUFFIXES
    global _WORKER_LANGUAGE_MODE
    global _WORKER_INCLUDE_STREAMING
    global _WORKER_PROFILES
    global _WORKER_SHARED_FOUND_VERSIONS
    global _WORKER_STOP_SIGNAL
    _WORKER_DEVICE_ID = int(device_id)
    _WORKER_BATCH_SIZE = max(1, int(batch_size))
    _WORKER_PRODUCER_COUNT = max(1, int(producer_count))
    _WORKER_PREFETCH_BATCHES = max(0, int(prefetch_batches))
    _WORKER_EXTENSION = extension
    _WORKER_ENTRIES = entries
    _WORKER_HASH_VIEW = _SharedHashView(hash_descriptor)
    _WORKER_GROUP_HASHES = _WORKER_HASH_VIEW
    _WORKER_INCLUDE_PLATFORM_SUFFIXES = include_platform_suffixes
    _WORKER_LANGUAGE_MODE = language_mode
    _WORKER_INCLUDE_STREAMING = include_streaming
    _WORKER_PROFILES = profiles
    _WORKER_SHARED_FOUND_VERSIONS = shared_found_versions
    _WORKER_STOP_SIGNAL = stop_signal
    _WORKER_PREPARED_BASES = prepare_gpu_bases(
        entries,
        extension,
        include_platform_suffixes,
        language_mode,
        include_streaming,
        profiles,
    )
    atexit.register(_close_worker_hash_view)


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
        producer_count=_WORKER_PRODUCER_COUNT,
        prefetch_batches=_WORKER_PREFETCH_BATCHES,
        prepared_bases=_WORKER_PREPARED_BASES,
    )
    return _GpuTaskResult(
        device_id=_WORKER_DEVICE_ID,
        versions=versions,
        matches=matches,
        scanned_candidates=scanned_candidates,
        logs=logs,
        cancelled=cancelled,
    )


def _cuda_owner_devices(device_ids: Iterable[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for device_id in device_ids:
        device_id = int(device_id)
        if device_id in seen:
            continue
        seen.add(device_id)
        out.append(device_id)
    return out


def _next_version_chunk(
    pending_chunks: deque[tuple[int, ...]],
    chunk_iterator,
) -> tuple[int, ...] | None:
    if pending_chunks:
        return pending_chunks.popleft()
    try:
        return next(chunk_iterator)
    except StopIteration:
        return None


def _dynamic_version_chunk_size(
    entries: list[RawPathEntry],
    extension: str,
    plan: VersionPlan,
    batch_sizes_by_device: dict[int, int],
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict],
    max_version_chunk_size: int,
    prefetch_batches_per_device: int,
) -> int:
    if plan.count <= 0:
        return 1
    per_version_candidates = candidate_count_for_entries(
        entries,
        extension,
        1,
        include_platform_suffixes,
        language_mode,
        include_streaming,
        profiles,
    )
    if per_version_candidates <= 0:
        return max(1, int(max_version_chunk_size))
    min_batch_size = min(batch_sizes_by_device.values()) if batch_sizes_by_device else 16384
    target_candidates = max(1, int(min_batch_size)) * max(1, int(prefetch_batches_per_device or 1))
    return max(1, min(int(max_version_chunk_size), target_candidates // per_version_candidates or 1))


def _create_shared_hash_data(hashes: set[int]) -> _SharedHashData:
    values = array("Q", sorted(int(value) & 0xFFFF_FFFF_FFFF_FFFF for value in hashes))
    size = max(1, len(values) * values.itemsize)
    memory = shared_memory.SharedMemory(create=True, size=size)
    if values:
        memory.buf[: len(values) * values.itemsize] = values.tobytes()
    return _SharedHashData(memory, _SharedHashDescriptor(memory.name, len(values)))


def _close_worker_hash_view() -> None:
    global _WORKER_HASH_VIEW
    if _WORKER_HASH_VIEW is not None:
        _WORKER_HASH_VIEW.close()
        _WORKER_HASH_VIEW = None


def _emit_task_logs(task_result: _GpuTaskResult, progress: ProgressCallback | None) -> None:
    if not progress:
        return
    for message in task_result.logs:
        progress(message)


def _record_task_matches(
    out: list[SuffixDiscoveryMatch],
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
            SuffixDiscoveryMatch(
                extension=extension,
                version=version,
                raw_path=raw_path,
                full_path=full_path,
            )
        )
