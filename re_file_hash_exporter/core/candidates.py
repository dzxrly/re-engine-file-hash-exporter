from __future__ import annotations

from typing import Iterable

from .constants import (
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

CandidateParts = tuple[str, ...]


def normalize_language_mode(language_mode: str, include_languages: bool = True) -> str:
    if not include_languages:
        return LANGUAGE_MODE_OFF
    normalized = str(language_mode or LANGUAGE_MODE_LOCALIZED).lower()
    if normalized in LANGUAGE_MODES:
        return normalized
    return LANGUAGE_MODE_LOCALIZED


def should_search_languages(
    extension: str,
    raw_path: str,
    language_mode: str,
    profiles: dict[str, dict] | None = None,
) -> bool:
    language_mode = normalize_language_mode(language_mode)
    if language_mode == LANGUAGE_MODE_OFF:
        return False
    if language_mode == LANGUAGE_MODE_ALL:
        return True

    normalized_extension = extension.lower().lstrip(".")
    profile = (profiles or {}).get(normalized_extension, {})
    if profile.get("language_search") is True:
        return True
    if normalized_extension in LOCALIZED_RESOURCE_EXTENSIONS:
        return True

    normalized_path = "/" + raw_path.replace("\\", "/").lower().lstrip("/")
    return any(keyword in normalized_path for keyword in LOCALIZED_PATH_KEYWORDS)


def candidate_count(
    raw_paths: Iterable[str],
    extension: str,
    version_count: int,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None = None,
) -> int:
    raw_path_count = 0
    raw_variants = 2 if include_streaming else 1
    base_variants = len(DEFAULT_PREFIXES) * (1 + (len(DEFAULT_PLATFORM_SUFFIXES) if include_platform_suffixes else 0))
    total = 0
    for raw_path in raw_paths:
        raw_path_count += 1
        language_variants = 1 + (
            len(LANGUAGES)
            if should_search_languages(extension, raw_path, language_mode, profiles)
            else 0
        )
        total += raw_variants * base_variants * language_variants
    if raw_path_count == 0:
        return 0
    return total * version_count


def iter_candidate_parts(
    raw_path: str,
    extension: str,
    version: int,
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None = None,
):
    raw_variants = [raw_path]
    if include_streaming:
        raw_variants.append(f"streaming/{raw_path}")

    version_text = str(version)
    use_languages = should_search_languages(extension, raw_path, language_mode, profiles)
    for prefix in DEFAULT_PREFIXES:
        for raw_variant in raw_variants:
            base_parts: CandidateParts = (prefix, raw_variant, ".", version_text)
            yield base_parts
            if use_languages:
                for language in LANGUAGES:
                    yield (*base_parts, ".", language)
            if include_platform_suffixes:
                for suffix in DEFAULT_PLATFORM_SUFFIXES:
                    platform_parts: CandidateParts = (*base_parts, ".", suffix)
                    yield platform_parts
                    if use_languages:
                        for language in LANGUAGES:
                            yield (*platform_parts, ".", language)


def join_candidate_parts(parts: CandidateParts) -> str:
    return "".join(parts)
