from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..constants import IGNORED_RESOURCE_EXTENSIONS, LANGUAGE_SEARCH_SUFFIXES
from ..models import SuffixDiscoveryOptions, SuffixCounts
from .plan import VersionPlan, date_code_plan, discrete_version_plan, empty_version_plan, numeric_range_plan

PROFILE_FILE_NAME = "file_suffix_profiles.json"
MIN_DATE_CODE_DATE = date(2000, 1, 1)
DATE_END_TODAY = "today"


def profile_file_candidates() -> list[Path]:
    project_root = Path(__file__).resolve().parents[3]
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


def load_profile_languages() -> list[str]:
    for path in profile_file_candidates():
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"{PROFILE_FILE_NAME}: expected a JSON object.")
            return profile_languages_from_data(data, source=str(path))
    return list(LANGUAGE_SEARCH_SUFFIXES)


def profile_languages_from_data(data: dict[str, Any], source: str = PROFILE_FILE_NAME) -> list[str]:
    values = data.get("languages")
    if values is None:
        return list(LANGUAGE_SEARCH_SUFFIXES)
    if not isinstance(values, list):
        raise ValueError(f"{source}: `languages` must be a string array.")

    languages: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{source}: `languages` must contain only strings.")
        language = value.strip()
        if not language or language in seen:
            continue
        languages.append(language)
        seen.add(language)

    return languages or list(LANGUAGE_SEARCH_SUFFIXES)


def default_date_range() -> tuple[str, str]:
    return "0", "0"


def is_today_date_end(text: str) -> bool:
    return text.strip().lower() == DATE_END_TODAY


def extension_uses_date_profile(extension: str, profiles: dict[str, dict[str, Any]] | None = None) -> bool:
    profiles = profiles if profiles is not None else load_version_profiles()
    return _profile_suffix_type(profiles.get(_normalize_extension(extension))) == "date_code"


def any_extension_uses_date_profile(extensions: list[str]) -> bool:
    profiles = load_version_profiles()
    return any(extension_uses_date_profile(extension, profiles) for extension in extensions)


def build_auto_detect_version_plan(
    extension: str,
    known_suffixes: SuffixCounts,
    options: SuffixDiscoveryOptions,
    profiles: dict[str, dict[str, Any]],
) -> VersionPlan:
    normalized = _normalize_extension(extension)
    profile = profiles.get(normalized)
    suffix_type = _profile_suffix_type(profile)

    if suffix_type == "exact":
        return _range_version_plan(options, _priority_versions(profile), "legacy exact-as-priority range")

    if suffix_type == "date_code":
        if profile is None:
            return empty_version_plan("missing date_code profile")
        return _date_code_version_plan(profile, options)

    if suffix_type == "adaptive":
        adaptive = _adaptive_version_plan(normalized, known_suffixes, options)
        if adaptive.count:
            return adaptive

    description = "numeric priority range preset" if profile else "numeric Min/Max fallback"
    return _range_version_plan(options, _priority_versions(profile), description)


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


def _range_version_plan(
    options: SuffixDiscoveryOptions,
    priority_versions: list[int] | None = None,
    description: str = "numeric range",
) -> VersionPlan:
    priority = _ordered_unique(priority_versions or [])
    if priority:
        lower_delta = max(0, int(options.min_version))
        upper_delta = max(0, int(options.max_version))
        start = max(0, min(priority) - lower_delta)
        end = max(start, max(priority) + upper_delta)
    else:
        start = max(0, int(options.min_version))
        end = max(start, int(options.max_version))
    return numeric_range_plan(start, end, priority, description)


def _adaptive_version_plan(
    extension: str,
    known_suffixes: SuffixCounts,
    options: SuffixDiscoveryOptions,
) -> VersionPlan:
    values: set[int] = set()
    for known in known_suffixes.get(extension, {}):
        start = max(0, known - options.neighbor_radius)
        end = known + options.neighbor_radius
        values.update(range(start, end + 1))
    return discrete_version_plan(sorted(values), "adaptive known-neighbor range")


def _date_code_version_plan(profile: dict[str, Any], options: SuffixDiscoveryOptions) -> VersionPlan:
    if str(profile.get("date_format", "YYMMDD")).upper() != "YYMMDD":
        raise ValueError("Only YYMMDD date_code profiles are currently supported.")

    base_dates, start_date, end_date = _date_code_bounds(profile, options)

    return date_code_plan(
        start_date=start_date,
        end_date=end_date,
        tail_width=int(profile.get("tail_width", 3)),
        priority_dates=base_dates,
        priority_tails=_priority_tails(profile),
        description="date_code priority range preset",
    )


def _date_code_bounds(profile: dict[str, Any], options: SuffixDiscoveryOptions) -> tuple[list[date], date, date]:
    base_dates = _priority_dates(profile)
    if not base_dates:
        start_date, end_date = _legacy_date_range(profile, options)
        return base_dates, start_date, end_date

    lower_delta = _parse_day_offset(options.date_start, "Date -days")
    start_date = max(MIN_DATE_CODE_DATE, min(base_dates) - timedelta(days=lower_delta))
    end_date = _date_code_end_date(max(base_dates), options.date_end)
    return base_dates, start_date, max(start_date, end_date)


def _date_code_end_date(latest_priority_date: date, text: str) -> date:
    if is_today_date_end(text):
        return max(latest_priority_date, _local_today())
    upper_delta = _parse_day_offset(text, "Date +days")
    return latest_priority_date + timedelta(days=upper_delta)


def _local_today() -> date:
    return date.today()


def _legacy_date_range(profile: dict[str, Any], options: SuffixDiscoveryOptions) -> tuple[date, date]:
    start_text = str(profile.get("default_date_start", "")).strip()
    end_text = str(profile.get("default_date_end", start_text)).strip()
    if start_text and end_text:
        start_date = _parse_date_value(start_text, "default_date_start")
        end_date = _parse_date_value_or_today(end_text, "default_date_end")
    else:
        start_date = _parse_date_option(options.date_start, "Date from")
        end_date = _parse_date_option(options.date_end, "Date to", allow_today=True)
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def _parse_date_option(text: str, label: str, allow_today: bool = False) -> date:
    value = text.strip()
    if not value:
        raise ValueError(f"{label} is required for auto_detect date_code profiles.")
    if allow_today:
        return _parse_date_value_or_today(value, label)
    return _parse_date_value(value, label)


def _parse_date_value_or_today(value: str, label: str) -> date:
    if is_today_date_end(value):
        return _local_today()
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
