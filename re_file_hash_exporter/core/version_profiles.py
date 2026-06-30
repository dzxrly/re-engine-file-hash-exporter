from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import BruteForceOptions, SuffixCounts

PROFILE_FILE_NAME = "file_suffix_profiles.json"


def profile_file_candidates() -> list[Path]:
    project_root = Path(__file__).resolve().parents[2]
    cwd_path = Path.cwd() / PROFILE_FILE_NAME
    project_path = project_root / PROFILE_FILE_NAME
    if cwd_path == project_path:
        return [cwd_path]
    return [cwd_path, project_path]


def load_version_profiles() -> dict[str, dict[str, Any]]:
    for path in profile_file_candidates():
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            extensions = data.get("extensions", {})
            if not isinstance(extensions, dict):
                raise ValueError(f"{PROFILE_FILE_NAME}: `extensions` must be an object.")
            return {
                str(extension).lower().lstrip("."): profile
                for extension, profile in extensions.items()
                if isinstance(profile, dict)
            }
    return {}


def default_date_range() -> tuple[str, str]:
    profiles = load_version_profiles()
    for profile in profiles.values():
        if _profile_suffix_type(profile) == "date_code":
            start = str(profile.get("default_date_start", "")).strip()
            end = str(profile.get("default_date_end", start)).strip()
            if start and end:
                return start, end
    return "", ""


def extension_uses_date_profile(extension: str, profiles: dict[str, dict[str, Any]] | None = None) -> bool:
    profiles = profiles if profiles is not None else load_version_profiles()
    return _profile_suffix_type(profiles.get(_normalize_extension(extension))) == "date_code"


def any_extension_uses_date_profile(extensions: list[str]) -> bool:
    profiles = load_version_profiles()
    return any(extension_uses_date_profile(extension, profiles) for extension in extensions)


def plan_auto_detect_versions(
    extension: str,
    known_suffixes: SuffixCounts,
    options: BruteForceOptions,
    profiles: dict[str, dict[str, Any]],
) -> list[int]:
    normalized = _normalize_extension(extension)
    profile = profiles.get(normalized)
    suffix_type = _profile_suffix_type(profile)

    if suffix_type == "exact":
        return _range_versions(options, _priority_versions(profile))

    if suffix_type == "date_code":
        if profile is None:
            return []
        return _date_code_versions(profile, options)

    if suffix_type == "adaptive":
        adaptive = _adaptive_versions(normalized, known_suffixes, options)
        if adaptive:
            return adaptive

    return _range_versions(options, _priority_versions(profile))


def describe_auto_profile(extension: str, profiles: dict[str, dict[str, Any]]) -> str:
    normalized = _normalize_extension(extension)
    profile = profiles.get(normalized)
    suffix_type = _profile_suffix_type(profile)
    if profile is None:
        return "no preset, numeric Min/Max fallback"
    if suffix_type == "date_code":
        return "date_code priority preset"
    if suffix_type == "exact":
        return "legacy exact-as-priority preset"
    if suffix_type == "adaptive":
        return "adaptive preset"
    return "numeric priority preset"


def _normalize_extension(extension: str) -> str:
    return extension.lower().lstrip(".")


def _profile_suffix_type(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "numeric"
    return str(profile.get("suffix_type") or "numeric").lower()


def _range_versions(options: BruteForceOptions, priority_versions: list[int] | None = None) -> list[int]:
    start = max(0, int(options.min_version))
    end = max(start, int(options.max_version))
    priority = [version for version in priority_versions or [] if start <= version <= end]
    return _ordered_unique([*priority, *range(start, end + 1)])


def _adaptive_versions(
    extension: str,
    known_suffixes: SuffixCounts,
    options: BruteForceOptions,
) -> list[int]:
    values: set[int] = set()
    for known in known_suffixes.get(extension, {}):
        start = max(0, known - options.neighbor_radius)
        end = known + options.neighbor_radius
        values.update(range(start, end + 1))
    return sorted(values)


def _date_code_versions(profile: dict[str, Any], options: BruteForceOptions) -> list[int]:
    if str(profile.get("date_format", "YYMMDD")).upper() != "YYMMDD":
        raise ValueError("Only YYMMDD date_code profiles are currently supported.")

    start_date = _parse_date_option(options.date_start, "Date from")
    end_date = _parse_date_option(options.date_end, "Date to")
    if end_date < start_date:
        start_date, end_date = end_date, start_date

    tail_width = int(profile.get("tail_width", 3))
    all_tails = list(range(0, 10**tail_width))
    priority_tails = [tail for tail in _priority_tails(profile) if 0 <= tail < 10**tail_width]
    remainder_tails = [tail for tail in all_tails if tail not in set(priority_tails)]
    tail_phases = [priority_tails, remainder_tails] if priority_tails else [all_tails]
    dates: list[date] = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)

    values: list[int] = []
    for tails in tail_phases:
        for current in dates:
            date_prefix = current.strftime("%y%m%d")
            for tail in tails:
                values.append(int(f"{date_prefix}{tail:0{tail_width}d}"))
    return _ordered_unique(values)


def _parse_date_option(text: str, label: str) -> date:
    value = text.strip()
    if not value:
        raise ValueError(f"{label} is required for auto_detect date_code profiles.")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc


def _priority_versions(profile: dict[str, Any] | None) -> list[int]:
    if not profile:
        return []
    return _unique_ints(profile.get("priority_versions", profile.get("versions", [])))


def _priority_tails(profile: dict[str, Any]) -> list[int]:
    return _unique_ints(profile.get("priority_tails", profile.get("tail_values", [])))


def _unique_ints(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    return sorted({int(value) for value in values})


def _ordered_unique(values) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for value in values:
        value = int(value)
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
