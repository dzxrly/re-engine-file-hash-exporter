from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable

from ..models import BruteForceOptions, SuffixCounts
from ..pak_hash import PakHashGroup
from ..version_plan import VersionPlan, discrete_version_plan, numeric_range_plan
from ..version_profiles import build_auto_detect_version_plan, load_version_profiles
from .candidate_policy import candidate_count_for_entries
from .path_catalog import RawPathEntry
from .progress import BruteForceProgressTracker, ProgressCallback

CancelCallback = Callable[[], bool]

_CANCEL_CHECK_INTERVAL = 8192


@dataclass(slots=True)
class GroupPlan:
    versions_by_extension: dict[str, VersionPlan]
    candidate_counts_by_extension: dict[str, int]
    cancelled: bool = False


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
) -> VersionPlan:
    _raise_if_cancelled(cancel_requested)
    if options.mode == "custom":
        return discrete_version_plan(parse_custom_versions(options.custom_versions, cancel_requested), "custom versions")

    if options.mode == "auto_detect":
        profiles = auto_profiles if auto_profiles is not None else load_version_profiles()
        return build_auto_detect_version_plan(extension, known_suffixes, options, profiles)

    if options.mode == "adaptive":
        values: set[int] = set()
        for known in known_suffixes.get(extension, Counter()):
            _raise_if_cancelled(cancel_requested)
            start = max(0, known - options.neighbor_radius)
            end = known + options.neighbor_radius
            values.update(range(start, end + 1))
        if values:
            return discrete_version_plan(sorted(values), "adaptive known-neighbor range")

    start = max(0, int(options.min_version))
    end = max(start, int(options.max_version))
    return numeric_range_plan(start, end, description="numeric Min/Max range")


def plan_group(
    group: PakHashGroup,
    group_mode: str,
    incremental_group: bool,
    entries_by_extension: dict[str, list[RawPathEntry]],
    known_suffixes: SuffixCounts,
    max_versions_by_extension: dict[str, int],
    options: BruteForceOptions,
    profiles: dict[str, dict],
    auto_profiles: dict[str, dict],
    language_mode: str,
    tracker: BruteForceProgressTracker,
    progress: ProgressCallback | None,
    stopped: CancelCallback,
) -> GroupPlan:
    versions_by_extension: dict[str, VersionPlan] = {}
    candidate_counts_by_extension: dict[str, int] = {}
    planned_scan_count = 0
    planned_extension_count = 0
    tracker.set_phase(
        "planning",
        f"Planning {group.display_name} ({group_mode})",
        total_extensions=len(entries_by_extension),
        total_scan_count=0,
    )
    if progress:
        progress(
            "Planning candidate versions for: "
            + ", ".join(f".{extension}" for extension in sorted(entries_by_extension))
        )

    for extension, entries in entries_by_extension.items():
        if stopped():
            if progress:
                progress("Stop requested. Brute-force planning cancelled.")
            tracker.set_phase(
                "planning",
                "Cancelled while planning candidates",
                total_extensions=len(entries_by_extension),
                total_scan_count=0,
                completed_extensions=planned_extension_count,
                completed_scan_count=planned_scan_count,
            )
            return GroupPlan(versions_by_extension, candidate_counts_by_extension, cancelled=True)

        tracker.set_planning_progress(planned_extension_count, planned_scan_count, extension)
        if progress:
            progress(f"Planning .{extension} candidate versions for {group.display_name}...")
        try:
            plan = plan_versions_for_extension(
                extension,
                known_suffixes,
                options,
                auto_profiles,
                cancel_requested=stopped,
            )
        except InterruptedError:
            if progress:
                progress("Stop requested. Brute-force planning cancelled.")
            tracker.set_phase(
                "planning",
                "Cancelled while planning candidates",
                total_extensions=len(entries_by_extension),
                total_scan_count=0,
                completed_extensions=planned_extension_count,
                completed_scan_count=planned_scan_count,
            )
            return GroupPlan(versions_by_extension, candidate_counts_by_extension, cancelled=True)

        minimum_version = max_versions_by_extension.get(extension) if incremental_group else None
        if minimum_version is not None:
            plan = plan.with_minimum(minimum_version)
            if progress:
                progress(f".{extension}: incremental lower bound {minimum_version}.")
        elif incremental_group and progress:
            progress(f".{extension}: no known maximum yet, using full plan for this incremental PAK.")

        versions_by_extension[extension] = plan
        candidate_counts_by_extension[extension] = (
            candidate_count_for_entries(
                entries,
                extension,
                plan.count,
                options.include_platform_suffixes,
                language_mode,
                options.include_streaming,
                profiles,
            )
            if plan.count
            else 0
        )
        planned_scan_count += candidate_counts_by_extension[extension]
        planned_extension_count += 1
        if progress:
            progress(
                f".{extension}: planned {plan.count} version(s), "
                f"{candidate_counts_by_extension[extension]} path candidate scan(s) for {group.display_name}."
            )
        tracker.set_planning_progress(planned_extension_count, planned_scan_count, extension, force=True)

    return GroupPlan(versions_by_extension, candidate_counts_by_extension)


def _raise_if_cancelled(cancel_requested: CancelCallback | None) -> None:
    if cancel_requested and cancel_requested():
        raise InterruptedError("Brute-force planning was cancelled.")

