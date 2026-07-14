from __future__ import annotations

LANGUAGES: list[str] = [
    "Ja",
    "En",
    "Fr",
    "It",
    "De",
    "Es",
    "Ru",
    "Pl",
    "Nl",
    "Pt",
    "PtBR",
    "Ko",
    "ZhTW",
    "ZhCN",
    "Fi",
    "Sv",
    "Da",
    "No",
    "Cs",
    "Hu",
    "Sk",
    "Ar",
    "Tr",
    "Bu",
    "Gr",
    "Ro",
    "Th",
    "Uk",
    "Vi",
    "Id",
    "Fc",
    "Hi",
    "Es419",
]

LANGUAGE_SEARCH_SUFFIXES: list[str] = ["Ja", "En"]

DEFAULT_PREFIXES: list[str] = ["natives/STM/"]
DEFAULT_PLATFORM_SUFFIXES: list[str] = ["X64", "STM"]

IGNORED_RESOURCE_EXTENSIONS: set[str] = {"exe", "json"}

CANDIDATE_MODE_SMALL_RANGE = "small_range"
CANDIDATE_MODE_ADAPTIVE = "adaptive"
CANDIDATE_MODE_CUSTOM = "custom"
CANDIDATE_MODE_AUTO_DETECT = "auto_detect"
CANDIDATE_MODE_PROFILE_THEN_RANGE = "profile_then_range"
CANDIDATE_MODES: tuple[str, ...] = (
    CANDIDATE_MODE_SMALL_RANGE,
    CANDIDATE_MODE_ADAPTIVE,
    CANDIDATE_MODE_CUSTOM,
    CANDIDATE_MODE_AUTO_DETECT,
    CANDIDATE_MODE_PROFILE_THEN_RANGE,
)

LANGUAGE_MODE_OFF = "off"
LANGUAGE_MODE_LOCALIZED = "localized"
LANGUAGE_MODE_ALL = "all"
LANGUAGE_MODES: tuple[str, ...] = (
    LANGUAGE_MODE_LOCALIZED,
    LANGUAGE_MODE_OFF,
    LANGUAGE_MODE_ALL,
)

LOCALIZED_RESOURCE_EXTENSIONS: set[str] = {"asrc", "bnk", "msg", "pck", "sbnk", "spck"}
LOCALIZED_PATH_KEYWORDS: tuple[str, ...] = (
    "/caption/",
    "/dialog/",
    "/dialogue/",
    "/font/",
    "/localisation/",
    "/localise/",
    "/localization/",
    "/localize/",
    "/message/",
    "/msg/",
    "/speech/",
    "/subtitle/",
    "/text/",
    "/vo/",
    "/voice",
)

TAG_SUFFIXES: set[str] = {language.lower() for language in LANGUAGES} | {
    "x64",
    "stm",
    "nsw",
    "msg",
}

RESOURCE_PATH_PREFIXES: tuple[str, ...] = (
    "art/",
    "gamedesign/",
    "gui/",
    "mastermaterial/",
    "materialshader/",
    "motion/",
    "natives/",
    "script/",
    "sound/",
    "system/",
    "systems/",
)

MISSING_REPORT_SUFFIX = ".missing_versions.txt"
