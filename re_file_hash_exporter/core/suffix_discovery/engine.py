from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..gpu.torch_backend import match_extension_with_torch, resolve_cuda_devices
from ..models import SuffixDiscoveryMatch, SuffixDiscoveryOptions, SuffixDiscoveryResult, DmpScanResult, SuffixCounts
from ..pak.cache import PakHashCache
from ..pak.reader import PakHashGroup, load_hash_groups_from_paks
from ..search.candidate_policy import candidate_count_for_entries, normalize_language_mode
from ..search.gpu_pool import search_extension_multi_gpu
from ..search.path_catalog import RawPathEntry, collect_raw_path_entries_by_extension
from ..search.planning import GroupPlan, parse_custom_versions, plan_group, plan_versions_for_extension
from ..search.process_pool import CpuSearchExecutor
from ..search.progress import SuffixDiscoveryProgressTracker, ProgressCallback
from ..versions.plan import VersionPlan
from ..versions.profiles import describe_auto_profile, load_version_profiles, profile_baseline_max_version

CancelCallback = Callable[[], bool]

VERSION_CHUNK_SIZE = 4096
PROFILE_BASELINE_LABEL = "file_suffix_profiles.json"


def gpu_status_message(requested: bool, requested_devices: list[int] | None = None) -> tuple[bool, str | None, list[int]]:
    if not requested:
        return False, None, []
    ok, message, devices = resolve_cuda_devices(requested_devices)
    if ok:
        return True, message, devices
    return False, f"GPU requested but unavailable: {message} Falling back to CPU multiprocessing.", []


def discover_suffixes(
    scan: DmpScanResult,
    pak_paths: list[Path],
    known_suffixes: SuffixCounts,
    options: SuffixDiscoveryOptions,
    progress: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
    pak_cache: PakHashCache | None = None,
) -> SuffixDiscoveryResult:
    def stopped() -> bool:
        return bool(cancel_requested and cancel_requested())

    warnings: list[str] = []
    use_gpu = False
    gpu_devices: list[int] = []
    use_gpu, gpu_message, gpu_devices = gpu_status_message(options.request_gpu, options.gpu_devices)
    if gpu_message:
        if not use_gpu:
            warnings.append(gpu_message)
        if progress:
            progress(gpu_message)

    entries_by_extension = collect_raw_path_entries_by_extension(
        scan,
        options.selected_extensions,
        include_versioned_extensions=True,
    )
    if not entries_by_extension:
        return SuffixDiscoveryResult(warnings=["No raw path evidence found for selected extensions."])

    if stopped():
        return SuffixDiscoveryResult(cancelled=True)

    tracker = SuffixDiscoveryProgressTracker(
        progress,
        total_extensions=len(entries_by_extension),
        total_scan_count=0,
        phase="loading_paks",
        phase_detail=f"Loading metadata from {len(pak_paths)} PAK file(s)",
    )
    tracker.emit(force=True)

    if progress:
        progress("Loading PAK entry hashes...")
    pak_groups = _load_pak_groups(pak_paths, pak_cache, progress)
    if stopped():
        if progress:
            progress("Stop requested. Suffix discovery cancelled before matching.")
        tracker.set_phase("loading_paks", "Cancelled while loading PAK metadata")
        return SuffixDiscoveryResult(cancelled=True)

    if progress:
        total_hash_entries = sum(len(group.hashes) for group in pak_groups)
        progress(f"Loaded {total_hash_entries} PAK hash entries across {len(pak_groups)} PAK group(s).")

    result = SuffixDiscoveryResult(warnings=warnings)
    profiles = load_version_profiles()
    profile_baseline_versions = _profile_baseline_versions(entries_by_extension, profiles)
    auto_profiles = profiles if options.mode == "auto_detect" else {}
    language_mode = normalize_language_mode(options.language_mode, options.include_languages)
    processes = options.processes if options.processes and options.processes > 0 else os.cpu_count() or 1
    processes = max(1, processes)
    max_versions_by_group: dict[str, dict[str, int]] = {}
    discovered_versions_by_group: dict[str, dict[str, set[int]]] = {}
    baseline_groups: set[str] = set()

    for group_index, group in enumerate(pak_groups, start=1):
        if stopped():
            result.cancelled = True
            if progress:
                progress("Stop requested. Suffix discovery cancelled.")
            tracker.emit(force=True)
            return result

        group_mode, incremental_group, profile_baseline_group = _group_scan_mode(group, baseline_groups)
        if profile_baseline_group:
            _seed_profile_baseline_versions(max_versions_by_group, group.group_key, profile_baseline_versions)
        _report_group_start(
            group,
            group_index,
            len(pak_groups),
            group_mode,
            profile_baseline_group,
            profile_baseline_versions,
            progress,
        )
        group_discovered_versions = discovered_versions_by_group.setdefault(group.group_key, {})

        group_plan = plan_group(
            group=group,
            group_mode=group_mode,
            incremental_group=incremental_group,
            entries_by_extension=entries_by_extension,
            known_suffixes=known_suffixes,
            skip_versions_by_extension=group_discovered_versions,
            max_versions_by_extension=max_versions_by_group.get(group.group_key, {}) if incremental_group else {},
            options=options,
            profiles=profiles,
            auto_profiles=auto_profiles,
            language_mode=language_mode,
            tracker=tracker,
            progress=progress,
            stopped=stopped,
        )
        if group_plan.cancelled:
            result.cancelled = True
            return result

        tracker.set_phase(
            "searching",
            f"Searching {group.display_name} ({group_mode})",
            total_extensions=len(entries_by_extension),
            total_scan_count=sum(group_plan.candidate_counts_by_extension.values()),
        )
        _report_search_start(group, group_mode, group_plan, tracker, entries_by_extension, progress)

        cpu_executor: CpuSearchExecutor | None = None

        def get_cpu_executor() -> CpuSearchExecutor:
            nonlocal cpu_executor
            if cpu_executor is None:
                cpu_executor = CpuSearchExecutor(processes, group.hashes, profiles, stopped)
            return cpu_executor

        try:
            for extension, entries in entries_by_extension.items():
                if stopped():
                    result.cancelled = True
                    if progress:
                        progress("Stop requested. Suffix discovery cancelled.")
                    tracker.emit(force=True)
                    return result

                plan = group_plan.versions_by_extension[extension]
                if not plan.count:
                    if group_discovered_versions.get(extension):
                        if progress:
                            progress(f".{extension}: no new candidate versions to search for {group.display_name}.")
                    else:
                        result.warnings.append(f"No candidate versions planned for {extension} in {group.display_name}.")
                    tracker.finish_extension(extension)
                    continue

                before_match_count = len(result.matches)
                found_versions = group_discovered_versions.setdefault(extension, set())
                if use_gpu:
                    gpu_outcome = _search_extension_gpu(
                        extension=extension,
                        entries=entries,
                        plan=plan,
                        known_profile_text=describe_auto_profile(extension, auto_profiles),
                        group_hashes=group.hashes,
                        options=options,
                        profiles=profiles,
                        language_mode=language_mode,
                        tracker=tracker,
                        progress=progress,
                        cancel_requested=cancel_requested,
                        stopped=stopped,
                        found_versions=found_versions,
                        gpu_devices=gpu_devices,
                    )
                    result.matches.extend(gpu_outcome.matches)
                    use_gpu = gpu_outcome.gpu_available
                    if gpu_outcome.warning:
                        result.warnings.append(gpu_outcome.warning)
                    if gpu_outcome.cancelled:
                        result.cancelled = True
                        return result
                    if gpu_outcome.completed:
                        _update_group_max_versions(
                            max_versions_by_group,
                            group.group_key,
                            result.matches[before_match_count:],
                        )
                        continue

                _search_extension_cpu(
                    result=result,
                    cpu_executor=get_cpu_executor(),
                    extension=extension,
                    entries=entries,
                    plan=plan,
                    total_candidates=group_plan.candidate_counts_by_extension[extension],
                    known_profile_text=describe_auto_profile(extension, auto_profiles),
                    options=options,
                    language_mode=language_mode,
                    processes=processes,
                    tracker=tracker,
                    progress=progress,
                    found_versions=found_versions,
                )
                _update_group_max_versions(
                    max_versions_by_group,
                    group.group_key,
                    result.matches[before_match_count:],
                )
                if result.cancelled:
                    return result
        finally:
            if cpu_executor is not None:
                cpu_executor.shutdown()

    return result


@dataclass(slots=True)
class _GpuOutcome:
    matches: list[SuffixDiscoveryMatch]
    gpu_available: bool
    completed: bool
    cancelled: bool = False
    warning: str | None = None


def _load_pak_groups(
    pak_paths: list[Path],
    pak_cache: PakHashCache | None,
    progress: ProgressCallback | None,
) -> list[PakHashGroup]:
    workers = min(8, len(pak_paths))
    if pak_cache is not None:
        return pak_cache.load_groups(pak_paths, workers=workers, progress=progress)
    return load_hash_groups_from_paks(pak_paths, workers=workers, progress=progress)


def _group_scan_mode(group: PakHashGroup, baseline_groups: set[str]) -> tuple[str, bool, bool]:
    has_baseline_group = group.group_key in baseline_groups
    if group.is_incremental:
        if not has_baseline_group:
            baseline_groups.add(group.group_key)
            return "incremental", True, True
        return "incremental", True, False

    if not has_baseline_group:
        baseline_groups.add(group.group_key)
    return "full", False, False


def _profile_baseline_versions(
    entries_by_extension: dict[str, list[RawPathEntry]],
    profiles: dict[str, dict],
) -> dict[str, int]:
    out: dict[str, int] = {}
    for extension in entries_by_extension:
        baseline = profile_baseline_max_version(extension, profiles)
        if baseline is not None:
            out[extension] = baseline
    return out


def _seed_profile_baseline_versions(
    max_versions_by_group: dict[str, dict[str, int]],
    group_key: str,
    profile_baseline_versions: dict[str, int],
) -> None:
    if not profile_baseline_versions:
        return
    group_versions = max_versions_by_group.setdefault(group_key, {})
    for extension, version in profile_baseline_versions.items():
        current = group_versions.get(extension)
        if current is None or version > current:
            group_versions[extension] = version


def _report_group_start(
    group: PakHashGroup,
    group_index: int,
    total_groups: int,
    group_mode: str,
    profile_baseline_group: bool,
    profile_baseline_versions: dict[str, int],
    progress: ProgressCallback | None,
) -> None:
    if not progress:
        return
    if profile_baseline_group:
        if profile_baseline_versions:
            baseline_text = ", ".join(
                f".{extension}>{version}" for extension, version in sorted(profile_baseline_versions.items())
            )
            progress(
                f"PAK group [{group_index}/{total_groups}] {group.display_name}: "
                f"no base PAK was loaded before this patch, using {PROFILE_BASELINE_LABEL} baseline "
                f"({baseline_text})."
            )
        else:
            progress(
                f"PAK group [{group_index}/{total_groups}] {group.display_name}: "
                f"no base PAK was loaded before this patch, using incremental scan; "
                f"no {PROFILE_BASELINE_LABEL} baseline was available for selected extensions."
            )
    progress(
        f"PAK group [{group_index}/{total_groups}] {group.display_name}: "
        f"{group_mode} scan over {len(group.hashes)} hashes."
    )


def _report_search_start(
    group: PakHashGroup,
    group_mode: str,
    group_plan: GroupPlan,
    tracker: SuffixDiscoveryProgressTracker,
    entries_by_extension: dict[str, list[RawPathEntry]],
    progress: ProgressCallback | None,
) -> None:
    if not progress:
        return
    progress(
        f"Planned {tracker.total_scan_count} path candidate scan(s) across "
        f"{tracker.total_extensions} extension(s) for {group.display_name}."
    )
    for extension, scan_count in _largest_candidate_counts(group_plan.candidate_counts_by_extension):
        progress(
            f"Plan hot spot {group.display_name}: .{extension} -> "
            f"{group_plan.versions_by_extension[extension].count} version(s), {scan_count} path candidate scan(s)."
        )
    progress(
        f"Suffix discovery started for {group.display_name} ({group_mode}): "
        + ", ".join(f".{extension}" for extension in sorted(entries_by_extension))
    )


def _search_extension_gpu(
    extension: str,
    entries: list[RawPathEntry],
    plan: VersionPlan,
    known_profile_text: str,
    group_hashes: set[int],
    options: SuffixDiscoveryOptions,
    profiles: dict[str, dict],
    language_mode: str,
    tracker: SuffixDiscoveryProgressTracker,
    progress: ProgressCallback | None,
    cancel_requested: CancelCallback | None,
    stopped: CancelCallback,
    found_versions: set[int],
    gpu_devices: list[int],
) -> _GpuOutcome:
    if options.mode == "auto_detect" and progress:
        progress(f".{extension}: auto_detect using {known_profile_text}.")
    devices = gpu_devices or [0]
    producers_per_device = _gpu_producers_per_device(options)
    prefetch_batches_per_device = max(0, int(options.gpu_prefetch_batches_per_device or 0))
    batch_sizes_by_device = _gpu_batch_sizes_by_device(options, devices)
    if len(devices) > 1:
        if progress:
            device_text = ", ".join(f"cuda:{device}" for device in devices)
            progress(
                f"Suffix discovery .{extension}: {len(entries)} raw paths x {plan.count} versions "
                f"using multi-GPU torch CUDA on {device_text} "
                f"(1 CUDA owner/device, {producers_per_device} producer(s)/device, "
                f"prefetch {prefetch_batches_per_device})."
            )
            progress(f".{extension}: version candidates {_format_candidate_range(plan)} ({plan.count} total).")
        try:
            outcome = search_extension_multi_gpu(
                extension=extension,
                entries=entries,
                plan=plan,
                group_hashes=group_hashes,
                device_ids=devices,
                batch_sizes_by_device=batch_sizes_by_device,
                producers_per_device=producers_per_device,
                prefetch_batches_per_device=prefetch_batches_per_device,
                include_platform_suffixes=options.include_platform_suffixes,
                language_mode=language_mode,
                include_streaming=options.include_streaming,
                profiles=profiles,
                version_chunk_size=VERSION_CHUNK_SIZE,
                tracker=tracker,
                progress=progress,
                stopped=stopped,
                found_versions=found_versions,
            )
        except InterruptedError:
            if progress:
                progress("Stop requested. Suffix discovery cancelled.")
            tracker.emit(force=True)
            return _GpuOutcome([], gpu_available=True, completed=True, cancelled=True)
        warning = "; ".join(outcome.warnings) if outcome.warnings else None
        return _GpuOutcome(
            matches=outcome.matches,
            gpu_available=outcome.completed,
            completed=outcome.completed,
            cancelled=outcome.cancelled,
            warning=warning,
        )

    if progress:
        progress(
            f"Suffix discovery .{extension}: {len(entries)} raw paths x {plan.count} versions "
            f"using torch CUDA on cuda:{devices[0]} "
            f"({producers_per_device} producer(s), prefetch {prefetch_batches_per_device})."
        )
        progress(f".{extension}: version candidates {_format_candidate_range(plan)} ({plan.count} total).")

    matches: list[SuffixDiscoveryMatch] = []
    try:
        chunk_size = _gpu_version_chunk_size(
            entries,
            extension,
            plan,
            batch_sizes_by_device.get(devices[0], options.gpu_batch_size),
            options.include_platform_suffixes,
            language_mode,
            options.include_streaming,
            profiles,
            prefetch_batches_per_device,
        )
        for version_chunk in plan.iter_chunks(chunk_size, stopped):
            gpu_matches, cancelled = match_extension_with_torch(
                extension=extension,
                entries=entries,
                versions=version_chunk,
                pak_hashes=group_hashes,
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
                batch_size=batch_sizes_by_device.get(devices[0], options.gpu_batch_size),
                found_versions=found_versions,
                device=f"cuda:{devices[0]}",
                producer_count=producers_per_device,
                prefetch_batches=prefetch_batches_per_device,
            )
            for raw_path, version, full_path in gpu_matches:
                matches.append(
                    SuffixDiscoveryMatch(
                        extension=extension,
                        version=version,
                        raw_path=raw_path,
                        full_path=full_path,
                    )
                )
            if cancelled:
                if progress:
                    progress("Suffix discovery stopped by user.")
                tracker.emit(force=True)
                return _GpuOutcome(matches, gpu_available=True, completed=True, cancelled=True)
    except RuntimeError as err:
        warning = f"GPU backend failed for .{extension}: {err}. Falling back to CPU."
        if progress:
            progress(warning)
        return _GpuOutcome(matches, gpu_available=False, completed=False, warning=warning)
    except InterruptedError:
        if progress:
            progress("Stop requested. Suffix discovery cancelled.")
        tracker.emit(force=True)
        return _GpuOutcome(matches, gpu_available=True, completed=True, cancelled=True)

    tracker.finish_extension(extension)
    return _GpuOutcome(matches, gpu_available=True, completed=True)


def _search_extension_cpu(
    result: SuffixDiscoveryResult,
    cpu_executor: CpuSearchExecutor,
    extension: str,
    entries: list[RawPathEntry],
    plan: VersionPlan,
    total_candidates: int,
    known_profile_text: str,
    options: SuffixDiscoveryOptions,
    language_mode: str,
    processes: int,
    tracker: SuffixDiscoveryProgressTracker,
    progress: ProgressCallback | None,
    found_versions: set[int],
) -> None:
    if options.mode == "auto_detect" and progress:
        progress(f".{extension}: auto_detect using {known_profile_text}.")
    if progress:
        progress(
            f"Suffix discovery .{extension}: {len(entries)} raw paths x {plan.count} versions "
            f"({total_candidates} path candidates) using {processes} processes."
        )
        progress(f".{extension}: version candidates {_format_candidate_range(plan)} ({plan.count} total).")

    outcome = cpu_executor.search_extension(
        extension=extension,
        entries=entries,
        plan=plan,
        include_platform_suffixes=options.include_platform_suffixes,
        language_mode=language_mode,
        include_streaming=options.include_streaming,
        version_chunk_size=VERSION_CHUNK_SIZE,
        tracker=tracker,
        progress=progress,
        found_versions=found_versions,
    )
    result.matches.extend(outcome.matches)
    result.cancelled = outcome.cancelled


def _gpu_batch_sizes_by_device(options: SuffixDiscoveryOptions, devices: list[int]) -> dict[int, int]:
    default = max(1, int(options.gpu_batch_size or 16384))
    out = {int(device): default for device in devices}
    for device, size in options.gpu_batch_sizes.items():
        device = int(device)
        if device in out:
            out[device] = max(1, int(size))
    return out


def _gpu_producers_per_device(options: SuffixDiscoveryOptions) -> int:
    explicit = int(options.gpu_producers_per_device or 0)
    if explicit > 0:
        return explicit
    return max(1, int(options.gpu_workers_per_device or 1))


def _gpu_version_chunk_size(
    entries: list[RawPathEntry],
    extension: str,
    plan: VersionPlan,
    batch_size: int,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict],
    prefetch_batches: int,
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
        return VERSION_CHUNK_SIZE
    target_candidates = max(1, int(batch_size)) * max(1, int(prefetch_batches or 1))
    return max(1, min(VERSION_CHUNK_SIZE, target_candidates // per_version_candidates or 1))


def _update_group_max_versions(
    max_versions_by_group: dict[str, dict[str, int]],
    group_key: str,
    matches: list[SuffixDiscoveryMatch],
) -> None:
    if not matches:
        return
    group_versions = max_versions_by_group.setdefault(group_key, {})
    _update_max_versions(group_versions, matches)


def _update_max_versions(max_versions: dict[str, int], matches: list[SuffixDiscoveryMatch]) -> None:
    for match in matches:
        current = max_versions.get(match.extension)
        if current is None or match.version > current:
            max_versions[match.extension] = match.version


def _largest_candidate_counts(candidate_counts_by_ext: dict[str, int], limit: int = 5) -> list[tuple[str, int]]:
    return sorted(candidate_counts_by_ext.items(), key=lambda item: item[1], reverse=True)[:limit]


def _format_candidate_range(plan: VersionPlan) -> str:
    if not plan.count or plan.low is None or plan.high is None:
        return "none"
    if plan.low == plan.high:
        return str(plan.low)
    return f"{plan.low}..{plan.high}"
