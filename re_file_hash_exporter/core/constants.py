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

DEFAULT_PREFIXES: list[str] = ["natives/STM/"]
DEFAULT_PLATFORM_SUFFIXES: list[str] = ["X64", "STM"]

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
