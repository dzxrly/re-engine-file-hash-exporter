from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..constants import (
    DEFAULT_PLATFORM_SUFFIXES,
    DEFAULT_PREFIXES,
    LANGUAGES,
    LANGUAGE_MODE_ALL,
    LANGUAGE_MODE_LOCALIZED,
    LANGUAGE_MODE_OFF,
    LANGUAGE_MODES,
    LOCALIZED_PATH_KEYWORDS,
    LOCALIZED_RESOURCE_EXTENSIONS,
)
from .path_catalog import RawPathEntry


@dataclass(frozen=True, slots=True)
class CandidateBase:
    raw_path: str
    base_text: str
    platform_suffixes: tuple[str, ...]
    language_suffixes: tuple[str, ...]


def normalize_language_mode(language_mode: str, include_languages: bool = True) -> str:
    if not include_languages:
        return LANGUAGE_MODE_OFF
    normalized = str(language_mode or LANGUAGE_MODE_LOCALIZED).lower()
    if normalized in LANGUAGE_MODES:
        return normalized
    return LANGUAGE_MODE_LOCALIZED


def candidate_count_for_entries(
    entries: Iterable[RawPathEntry],
    extension: str,
    version_count: int,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None = None,
) -> int:
    if version_count <= 0:
        return 0
    total = 0
    for entry in entries:
        for base in iter_candidate_bases(
            entry,
            extension,
            include_platform_suffixes,
            language_mode,
            include_streaming,
            profiles,
        ):
            total += 1
            total += len(base.language_suffixes)
            total += len(base.platform_suffixes)
            total += len(base.platform_suffixes) * len(base.language_suffixes)
    return total * version_count


def iter_candidate_bases(
    entry: RawPathEntry,
    extension: str,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None = None,
):
    profile = _profile_for(extension, profiles)
    platform_suffixes = _platform_suffixes(entry, include_platform_suffixes, profile)
    language_suffixes = _language_suffixes(extension, entry.raw_path, language_mode, profile)

    for prefix in DEFAULT_PREFIXES:
        for raw_variant in _raw_variants(entry, include_streaming, profile):
            yield CandidateBase(
                raw_path=entry.raw_path,
                base_text=f"{prefix}{raw_variant}.",
                platform_suffixes=platform_suffixes,
                language_suffixes=language_suffixes,
            )


def should_search_languages(
    extension: str,
    raw_path: str,
    language_mode: str,
    profiles: dict[str, dict] | None = None,
) -> bool:
    return bool(_language_suffixes(extension, raw_path, language_mode, _profile_for(extension, profiles)))


def _profile_for(extension: str, profiles: dict[str, dict] | None) -> dict:
    return (profiles or {}).get(extension.lower().lstrip("."), {})


def _raw_variants(entry: RawPathEntry, include_streaming: bool, profile: dict) -> tuple[str, ...]:
    streaming_policy = _profile_value(profile, "streaming_search")
    raw_path = entry.raw_path
    variants: list[str] = []

    include_plain = entry.seen_plain or not entry.seen_streaming or streaming_policy is True
    if include_plain:
        variants.append(raw_path)

    if include_streaming and _should_search_streaming(entry, streaming_policy):
        variants.append(f"streaming/{raw_path}")

    if not variants:
        variants.append(raw_path)
    return tuple(dict.fromkeys(variants))


def _should_search_streaming(entry: RawPathEntry, streaming_policy) -> bool:
    if streaming_policy is False:
        return False
    if streaming_policy is True:
        return True
    if isinstance(streaming_policy, str):
        normalized = streaming_policy.lower()
        if normalized in {"all", "true", "yes"}:
            return True
        if normalized in {"off", "false", "no"}:
            return False
    return entry.seen_streaming


def _platform_suffixes(entry: RawPathEntry, include_platform_suffixes: bool, profile: dict) -> tuple[str, ...]:
    if not include_platform_suffixes:
        return ()

    platform_policy = _profile_value(profile, "platform_search")
    if platform_policy is False:
        return ()
    if isinstance(platform_policy, list):
        allowed = {str(value).upper() for value in platform_policy}
        return tuple(suffix for suffix in DEFAULT_PLATFORM_SUFFIXES if suffix.upper() in allowed)
    if isinstance(platform_policy, str):
        normalized = platform_policy.lower()
        if normalized in {"off", "false", "no"}:
            return ()
        if normalized == "observed":
            observed = {suffix.upper() for suffix in entry.platform_suffixes}
            return tuple(suffix for suffix in DEFAULT_PLATFORM_SUFFIXES if suffix.upper() in observed)

    return tuple(DEFAULT_PLATFORM_SUFFIXES)


def _language_suffixes(extension: str, raw_path: str, language_mode: str, profile: dict) -> tuple[str, ...]:
    language_mode = normalize_language_mode(language_mode)
    language_policy = _profile_value(profile, "language_search")
    if language_policy is False or language_mode == LANGUAGE_MODE_OFF:
        return ()
    if language_mode == LANGUAGE_MODE_ALL or language_policy is True:
        return tuple(LANGUAGES)

    normalized_extension = extension.lower().lstrip(".")
    if normalized_extension in LOCALIZED_RESOURCE_EXTENSIONS:
        return tuple(LANGUAGES)

    normalized_path = "/" + raw_path.replace("\\", "/").lower().lstrip("/")
    if any(keyword in normalized_path for keyword in LOCALIZED_PATH_KEYWORDS):
        return tuple(LANGUAGES)
    return ()


def _profile_value(profile: dict, key: str):
    if not profile:
        return None
    return profile.get(key)

