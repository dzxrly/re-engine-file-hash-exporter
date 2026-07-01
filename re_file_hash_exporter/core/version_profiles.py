from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .constants import IGNORED_RESOURCE_EXTENSIONS
from .models import BruteForceOptions, SuffixCounts

PROFILE_FILE_NAME = "file_suffix_profiles.json"
MIN_DATE_CODE_DATE = date(2000, 1, 1)
CancelCallback = Callable[[], bool]
_CANCEL_CHECK_INTERVAL = 8192


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
                and str(extension).lower().lstrip(".") not in IGNORED_RESOURCE_EXTENSIONS
            }
    return {}


def default_date_range() -> tuple[str, str]:
    return "0", "0"


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
    cancel_requested: CancelCallback | None = None,
) -> list[int]:
    _raise_if_cancelled(cancel_requested)
    normalized = _normalize_extension(extension)
    profile = profiles.get(normalized)
    suffix_type = _profile_suffix_type(profile)

    if suffix_type == "exact":
        return _range_versions(options, _priority_versions(profile), cancel_requested)

    if suffix_type == "date_code":
        if profile is None:
            return []
        return _date_code_versions(profile, options, cancel_requested)

    if suffix_type == "adaptive":
        adaptive = _adaptive_versions(normalized, known_suffixes, options, cancel_requested)
        if adaptive:
            return adaptive

    return _range_versions(options, _priority_versions(profile), cancel_requested)


def describe_auto_profile(extension: str, profiles: dict[str, dict[str, Any]]) -> str:
    normalized = _normalize_extension(extension)
    profile = profiles.get(normalized)
    suffix_type = _profile_suffix_type(profile)
    if profile is None:
        return "no preset, numeric Min/Max fallback"
    if suffix_type == "date_code":
        return "date_code priority range preset"
    if suffix_type == "exact":
        return "legacy exact-as-priority preset"
    if suffix_type == "adaptive":
        return "adaptive preset"
    return "numeric priority range preset"


def _normalize_extension(extension: str) -> str:
    return extension.lower().lstrip(".")


def _profile_suffix_type(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "numeric"
    return str(profile.get("suffix_type") or "numeric").lower()


def _range_versions(
    options: BruteForceOptions,
    priority_versions: list[int] | None = None,
    cancel_requested: CancelCallback | None = None,
) -> list[int]:
    priority = _ordered_unique(priority_versions or [])
    if priority:
        lower_delta = max(0, int(options.min_version))
        upper_delta = max(0, int(options.max_version))
        start = max(0, min(priority) - lower_delta)
        end = max(start, max(priority) + upper_delta)
    else:
        start = max(0, int(options.min_version))
        end = max(start, int(options.max_version))

    values: list[int] = []
    seen: set[int] = set()
    for version in priority:
        _raise_if_cancelled(cancel_requested)
        if start <= version <= end and version not in seen:
            seen.add(version)
            values.append(version)

    for index, version in enumerate(range(start, end + 1), start=1):
        if index % _CANCEL_CHECK_INTERVAL == 0:
            _raise_if_cancelled(cancel_requested)
        if version in seen:
            continue
        seen.add(version)
        values.append(version)
    _raise_if_cancelled(cancel_requested)
    return values


def _adaptive_versions(
    extension: str,
    known_suffixes: SuffixCounts,
    options: BruteForceOptions,
    cancel_requested: CancelCallback | None = None,
) -> list[int]:
    values: set[int] = set()
    for known in known_suffixes.get(extension, {}):
        _raise_if_cancelled(cancel_requested)
        start = max(0, known - options.neighbor_radius)
        end = known + options.neighbor_radius
        for version in range(start, end + 1):
            values.add(version)
    return sorted(values)


def _date_code_versions(
    profile: dict[str, Any],
    options: BruteForceOptions,
    cancel_requested: CancelCallback | None = None,
) -> list[int]:
    if str(profile.get("date_format", "YYMMDD")).upper() != "YYMMDD":
        raise ValueError("Only YYMMDD date_code profiles are currently supported.")

    _raise_if_cancelled(cancel_requested)
    base_dates = _priority_dates(profile)
    if base_dates:
        lower_delta = _parse_day_offset(options.date_start, "Date -days")
        upper_delta = _parse_day_offset(options.date_end, "Date +days")
        start_date = max(MIN_DATE_CODE_DATE, min(base_dates) - timedelta(days=lower_delta))
        end_date = max(start_date, max(base_dates) + timedelta(days=upper_delta))
    else:
        start_date, end_date = _legacy_date_range(profile, options)

    tail_width = int(profile.get("tail_width", 3))
    all_tails = list(range(0, 10**tail_width))
    priority_tails = [tail for tail in _priority_tails(profile) if 0 <= tail < 10**tail_width]
    remainder_tails = [tail for tail in all_tails if tail not in set(priority_tails)]
    tail_phases = [priority_tails, remainder_tails] if priority_tails else [all_tails]
    all_dates = _date_range(start_date, end_date, cancel_requested)
    priority_dates = [current for current in base_dates if start_date <= current <= end_date]
    remainder_dates = [current for current in all_dates if current not in set(priority_dates)]
    date_phases = [priority_dates, remainder_dates] if priority_dates else [all_dates]

    values: list[int] = []
    count = 0
    for dates in date_phases:
        for tails in tail_phases:
            for current in dates:
                _raise_if_cancelled(cancel_requested)
                date_prefix = current.strftime("%y%m%d")
                for tail in tails:
                    values.append(int(f"{date_prefix}{tail:0{tail_width}d}"))
                    count += 1
                    if count % _CANCEL_CHECK_INTERVAL == 0:
                        _raise_if_cancelled(cancel_requested)
    _raise_if_cancelled(cancel_requested)
    return _ordered_unique(values)


def _date_range(
    start_date: date,
    end_date: date,
    cancel_requested: CancelCallback | None = None,
) -> list[date]:
    dates: list[date] = []
    current = start_date
    count = 0
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)
        count += 1
        if count % 32 == 0:
            _raise_if_cancelled(cancel_requested)
    return dates


def _legacy_date_range(profile: dict[str, Any], options: BruteForceOptions) -> tuple[date, date]:
    start_text = str(profile.get("default_date_start", "")).strip()
    end_text = str(profile.get("default_date_end", start_text)).strip()
    if start_text and end_text:
        start_date = _parse_date_value(start_text, "default_date_start")
        end_date = _parse_date_value(end_text, "default_date_end")
    else:
        start_date = _parse_date_option(options.date_start, "Date from")
        end_date = _parse_date_option(options.date_end, "Date to")
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _parse_date_option(text: str, label: str) -> date:
    value = text.strip()
    if not value:
        raise ValueError(f"{label} is required for auto_detect date_code profiles.")
    return _parse_date_value(value, label)


def _parse_date_value(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc


def _parse_day_offset(text: str, label: str) -> int:
    value = text.strip()
    if not value:
        return 0
    try:
        days = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a non-negative integer.") from exc
    if days < 0:
        raise ValueError(f"{label} must be a non-negative integer.")
    return days


def _priority_versions(profile: dict[str, Any] | None) -> list[int]:
    if not profile:
        return []
    return _unique_ints(profile.get("priority_versions", profile.get("versions", [])))


def _priority_tails(profile: dict[str, Any]) -> list[int]:
    return _unique_ints(profile.get("priority_tails", profile.get("tail_values", [])))


def _priority_dates(profile: dict[str, Any]) -> list[date]:
    values = profile.get("priority_dates", [])
    if not isinstance(values, list):
        return []
    dates: set[date] = set()
    for value in values:
        if isinstance(value, str):
            dates.add(_parse_date_value(value, "priority_dates"))
    return sorted(dates)


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


def _raise_if_cancelled(cancel_requested: CancelCallback | None) -> None:
    if cancel_requested and cancel_requested():
        raise InterruptedError("Brute-force planning was cancelled.")
