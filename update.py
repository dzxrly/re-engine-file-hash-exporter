from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from re_file_hash_exporter.core.config.builder import build_config_text
from re_file_hash_exporter.core.constants import IGNORED_RESOURCE_EXTENSIONS
from re_file_hash_exporter.core.dmp.parser import resource_suffix_from_path
from re_file_hash_exporter.core.versions.profiles import profile_languages_from_data


# Replace this with a GitHub-compatible mirror base URL when needed.
PROXY_URL = "https://github.com"

ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / ".runtime"
DEFAULT_JSON_PATH = ROOT / "file_suffix_profiles.json"
DEFAULT_TOML_PATH = ROOT / "universal_config.toml"
EKEY_REE_PAK_TOOL_REPOSITORY = "Ekey/REE.PAK.Tool.git"
EKEY_REE_PAK_TOOL_REF = "main"
MERGED_TOP_LEVEL_KEYS = ("version", "description", "languages")


class ProfileUpdateError(ValueError):
    pass


@dataclass
class ReeProjectListStats:
    files: int = 0
    lines: int = 0
    versioned_paths: int = 0
    new_extensions: set[str] = field(default_factory=set)
    updated_extensions: set[str] = field(default_factory=set)
    skipped_date_code_versions: int = 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Merge one or more TOML profile files into file_suffix_profiles.json, "
            "then export the current profiles as a reusable TOML file."
        )
    )
    parser.add_argument(
        "toml_paths",
        nargs="*",
        type=Path,
        help="TOML profile files to merge before exporting. If omitted, only export TOML.",
    )
    parser.add_argument(
        "--profiles-json",
        type=Path,
        default=DEFAULT_JSON_PATH,
        help=f"Profile JSON to update. Default: {DEFAULT_JSON_PATH}",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_TOML_PATH,
        help=f"TOML file to export. Default: {DEFAULT_TOML_PATH}",
    )
    parser.add_argument(
        "--ree-projects-github",
        "-r",
        action="store_true",
        help="Clone Ekey/REE.PAK.Tool and import all Projects/*.list files.",
    )
    args = parser.parse_args(argv)

    try:
        profiles_json = _resolve_user_path(args.profiles_json)
        output_toml = _resolve_user_path(args.output)
        data = load_profile_json(profiles_json)

        updated_extensions: list[str] = []
        for toml_path in args.toml_paths:
            source = _resolve_user_path(toml_path)
            incoming = load_profile_toml(source)
            updated_extensions.extend(merge_profile_data(data, incoming, source))

        ree_stats: list[tuple[str, ReeProjectListStats]] = []
        if args.ree_projects_github:
            stats = merge_ree_project_lists_from_github(data)
            ree_stats.append((f"Ekey/REE.PAK.Tool@{EKEY_REE_PAK_TOOL_REF}", stats))

        should_save_profiles_json = bool(args.toml_paths or ree_stats)
        if should_save_profiles_json:
            save_profile_json(profiles_json, data)

        if args.toml_paths:
            print(
                f"Updated {profiles_json} from {len(args.toml_paths)} TOML file(s); "
                f"{len(set(updated_extensions))} extension profile(s) touched."
            )
        for label, stats in ree_stats:
            print(
                f"Updated {profiles_json} from {label}; "
                f"{stats.files} list file(s), {stats.versioned_paths} versioned path(s), "
                f"{len(stats.updated_extensions)} extension profile(s) touched."
            )
            if stats.skipped_date_code_versions:
                print(
                    f"Skipped {stats.skipped_date_code_versions} non-date version(s) "
                    "for existing date_code profile(s)."
                )

        save_profile_toml(output_toml, data)
        print(f"Exported TOML profile config: {output_toml}")
        return 0
    except (
        OSError,
        ProfileUpdateError,
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"update.py: {exc}", file=sys.stderr)
        return 1


def load_profile_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProfileUpdateError(f"Profile JSON not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileUpdateError(f"{path}: expected a JSON object.")
    _ensure_mapping(data, "extensions", path)
    _remove_ignored_profile_extensions(data)
    return data


def load_profile_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProfileUpdateError(f"TOML config not found: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ProfileUpdateError(f"{path}: expected a TOML table.")
    return data


def merge_profile_data(target: dict[str, Any], incoming: Mapping[str, Any], source: Path | None = None) -> list[str]:
    label = source or Path("<toml>")
    updated_extensions: list[str] = []
    _remove_ignored_profile_extensions(target)

    for key in MERGED_TOP_LEVEL_KEYS:
        if key in incoming:
            target[key] = _normalize_value(incoming[key])

    if "suffix_types" in incoming:
        suffix_types = incoming["suffix_types"]
        if not isinstance(suffix_types, Mapping):
            raise ProfileUpdateError(f"{label}: suffix_types must be a table.")
        current = target.setdefault("suffix_types", {})
        if not isinstance(current, dict):
            raise ProfileUpdateError("file_suffix_profiles.json: suffix_types must be an object.")
        for key, value in suffix_types.items():
            current[str(key)] = _normalize_value(value)

    if "extensions" not in incoming:
        return updated_extensions

    incoming_extensions = incoming["extensions"]
    if not isinstance(incoming_extensions, Mapping):
        raise ProfileUpdateError(f"{label}: extensions must be a table.")

    target_extensions = target.setdefault("extensions", {})
    if not isinstance(target_extensions, dict):
        raise ProfileUpdateError("file_suffix_profiles.json: extensions must be an object.")

    for extension, profile in incoming_extensions.items():
        normalized_extension = _normalize_extension_key(extension, label)
        if normalized_extension in IGNORED_RESOURCE_EXTENSIONS:
            target_extensions.pop(normalized_extension, None)
            continue
        if not isinstance(profile, Mapping):
            raise ProfileUpdateError(f"{label}: extensions.{extension} must be a table.")
        current_profile = target_extensions.setdefault(normalized_extension, {})
        if not isinstance(current_profile, dict):
            current_profile = {}
            target_extensions[normalized_extension] = current_profile
        for key, value in profile.items():
            current_profile[str(key)] = _normalize_value(value)
        updated_extensions.append(normalized_extension)

    return updated_extensions


def merge_ree_project_lists_from_github(target: dict[str, Any]) -> ReeProjectListStats:
    sources = iter_github_ree_project_list_sources()
    return merge_ree_project_list_sources(target, sources)


def merge_ree_project_list_sources(
    target: dict[str, Any],
    sources: Iterable[tuple[str, Iterable[str]]],
) -> ReeProjectListStats:
    stats = ReeProjectListStats()
    versions_by_extension: dict[str, set[int]] = defaultdict(set)

    for _name, lines in sources:
        stats.files += 1
        for line in lines:
            stats.lines += 1
            suffix, _missing = resource_suffix_from_path(line)
            if not suffix:
                continue
            extension, version = suffix
            versions_by_extension[extension].add(version)
            stats.versioned_paths += 1

    stats.updated_extensions, stats.new_extensions, stats.skipped_date_code_versions = merge_ree_versions(
        target,
        versions_by_extension,
    )
    return stats


def merge_ree_versions(
    target: dict[str, Any],
    versions_by_extension: Mapping[str, set[int]],
) -> tuple[set[str], set[str], int]:
    _remove_ignored_profile_extensions(target)
    target_extensions = target.setdefault("extensions", {})
    if not isinstance(target_extensions, dict):
        raise ProfileUpdateError("file_suffix_profiles.json: extensions must be an object.")

    updated_extensions: set[str] = set()
    new_extensions: set[str] = set()
    skipped_date_code_versions = 0

    for extension, versions in sorted(versions_by_extension.items()):
        if not versions:
            continue
        normalized_extension = _normalize_extension_key(extension, Path("<ree-projects>"))
        if normalized_extension in IGNORED_RESOURCE_EXTENSIONS:
            target_extensions.pop(normalized_extension, None)
            continue
        existing_profile = target_extensions.get(normalized_extension)
        is_new_extension = not isinstance(existing_profile, dict)
        profile = existing_profile if isinstance(existing_profile, dict) else {}
        target_extensions[normalized_extension] = profile

        suffix_type = str(profile.get("suffix_type") or "").lower()
        if not suffix_type:
            if _versions_look_like_date_code(versions):
                suffix_type = "date_code"
                profile["suffix_type"] = suffix_type
                profile.setdefault("date_format", "YYMMDD")
                profile.setdefault("tail_width", _detect_date_code_tail_width(versions) or 3)
            else:
                suffix_type = "numeric"
                profile["suffix_type"] = suffix_type

        if suffix_type == "date_code":
            skipped_date_code_versions += _merge_date_code_versions(profile, versions)
        else:
            _merge_int_list(profile, "priority_versions", versions)

        updated_extensions.add(normalized_extension)
        if is_new_extension:
            new_extensions.add(normalized_extension)

    return updated_extensions, new_extensions, skipped_date_code_versions


def iter_github_ree_project_list_sources() -> Iterator[tuple[str, Iterator[str]]]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ree-pak-tool-", dir=RUNTIME_DIR) as temp_dir:
        clone_path = Path(temp_dir) / "REE.PAK.Tool"
        _clone_ree_pak_tool(clone_path)
        projects_dir = clone_path / "Projects"
        list_paths = sorted(projects_dir.rglob("*.list"))
        if not list_paths:
            raise ProfileUpdateError(f"No .list files found under: {projects_dir}")
        for list_path in list_paths:
            yield list_path.name, _iter_text_file_lines(list_path)


def save_profile_json(path: Path, data: Mapping[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, text)


def save_profile_toml(path: Path, data: Mapping[str, Any]) -> None:
    text = profile_data_to_toml(data)
    _atomic_write_text(path, text)


def profile_data_to_toml(data: Mapping[str, Any]) -> str:
    return build_config_text(profile_data_to_suffix_counts(data), languages=_profile_languages(data))


def profile_data_to_suffix_counts(data: Mapping[str, Any]) -> dict[str, Counter[int]]:
    suffix_counts: dict[str, Counter[int]] = {}
    extensions = data.get("extensions", {})
    if not isinstance(extensions, Mapping):
        raise ProfileUpdateError("file_suffix_profiles.json: extensions must be an object.")

    for extension, profile in extensions.items():
        normalized_extension = _normalize_extension_key(extension, Path("<profiles>"))
        if normalized_extension in IGNORED_RESOURCE_EXTENSIONS:
            continue
        if not isinstance(profile, Mapping):
            continue
        versions = _profile_versions(profile)
        if versions:
            suffix_counts[normalized_extension] = Counter({version: 1 for version in versions})
    return suffix_counts


def _profile_languages(data: Mapping[str, Any]) -> list[str]:
    try:
        return profile_languages_from_data(dict(data), source="file_suffix_profiles.json")
    except ValueError as exc:
        raise ProfileUpdateError(str(exc)) from exc


def _profile_versions(profile: Mapping[str, Any]) -> list[int]:
    suffix_type = str(profile.get("suffix_type") or "numeric").lower()
    if suffix_type == "date_code":
        return _date_code_profile_versions(profile)
    return sorted(_int_set(profile.get("priority_versions", profile.get("versions", []))))


def _date_code_profile_versions(profile: Mapping[str, Any]) -> list[int]:
    tail_width = _date_code_tail_width(profile)
    dates = _profile_priority_dates(profile)
    tails = sorted(_int_set(profile.get("priority_tails", profile.get("tail_values", []))))
    if not dates or not tails:
        return []

    versions: set[int] = set()
    for value in dates:
        prefix = int(value.strftime("%y%m%d"))
        for tail in tails:
            versions.add(prefix * (10**tail_width) + int(tail))
    return sorted(versions)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    return value


def _merge_date_code_versions(profile: dict[str, Any], versions: Iterable[int]) -> int:
    tail_width = _date_code_tail_width(profile)
    existing_dates = _string_set(profile.get("priority_dates", []))
    existing_tails = _int_set(profile.get("priority_tails", []))
    skipped = 0

    for version in versions:
        parsed = _parse_date_code_version(version, tail_width)
        if parsed is None:
            skipped += 1
            continue
        date_text, tail = parsed
        existing_dates.add(date_text)
        existing_tails.add(tail)

    profile["date_format"] = str(profile.get("date_format") or "YYMMDD")
    profile["tail_width"] = tail_width
    profile["priority_dates"] = sorted(existing_dates)
    profile["priority_tails"] = sorted(existing_tails)
    return skipped


def _merge_int_list(profile: dict[str, Any], key: str, values: Iterable[int]) -> None:
    merged = _int_set(profile.get(key, []))
    merged.update(int(value) for value in values)
    profile[key] = sorted(merged)


def _parse_date_code_version(version: int, tail_width: int) -> tuple[str, int] | None:
    text = str(int(version))
    if len(text) <= tail_width:
        return None
    date_text = text[: -tail_width]
    tail_text = text[-tail_width:]
    if len(date_text) != 6:
        return None
    try:
        parsed = datetime.strptime(date_text, "%y%m%d").date()
    except ValueError:
        return None
    if parsed < date(2000, 1, 1):
        return None
    return parsed.isoformat(), int(tail_text)


def _profile_priority_dates(profile: Mapping[str, Any]) -> list[date]:
    values = profile.get("priority_dates", [])
    if not isinstance(values, list):
        return []

    dates: set[date] = set()
    for value in values:
        if isinstance(value, date) and not isinstance(value, datetime):
            dates.add(value)
            continue
        if not isinstance(value, str):
            continue
        try:
            dates.add(datetime.strptime(value, "%Y-%m-%d").date())
        except ValueError as exc:
            raise ProfileUpdateError("priority_dates must use YYYY-MM-DD format.") from exc
    return sorted(dates)


def _versions_look_like_date_code(versions: Iterable[int]) -> bool:
    versions = set(versions)
    if not versions:
        return False
    tail_width = _detect_date_code_tail_width(versions)
    if tail_width is None:
        return False
    return all(_parse_date_code_version(version, tail_width) is not None for version in versions)


def _detect_date_code_tail_width(versions: Iterable[int]) -> int | None:
    versions = set(versions)
    if not versions:
        return None
    for tail_width in (3, 2, 1):
        if all(_parse_date_code_version(version, tail_width) is not None for version in versions):
            return tail_width
    return None


def _date_code_tail_width(profile: Mapping[str, Any]) -> int:
    value = profile.get("tail_width", 3)
    try:
        tail_width = int(value)
    except (TypeError, ValueError) as exc:
        raise ProfileUpdateError("date_code profile tail_width must be an integer.") from exc
    if tail_width <= 0:
        raise ProfileUpdateError("date_code profile tail_width must be positive.")
    return tail_width


def _string_set(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values}


def _int_set(values: Any) -> set[int]:
    if not isinstance(values, list):
        return set()
    return {int(value) for value in values}


def _normalize_extension_key(extension: Any, source: Path) -> str:
    normalized = str(extension).strip().lower().lstrip(".")
    if not normalized:
        raise ProfileUpdateError(f"{source}: extension names must not be empty.")
    return normalized


def _remove_ignored_profile_extensions(data: dict[str, Any]) -> None:
    extensions = data.get("extensions", {})
    if not isinstance(extensions, dict):
        return
    for extension in list(extensions):
        if str(extension).strip().lower().lstrip(".") in IGNORED_RESOURCE_EXTENSIONS:
            extensions.pop(extension, None)


def _ensure_mapping(data: Mapping[str, Any], key: str, source: Path) -> None:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ProfileUpdateError(f"{source}: {key} must be an object.")


def _resolve_user_path(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _ree_pak_tool_repository_url() -> str:
    return f"{PROXY_URL.rstrip('/')}/{EKEY_REE_PAK_TOOL_REPOSITORY}"


def _clone_ree_pak_tool(clone_path: Path) -> None:
    if clone_path.exists():
        shutil.rmtree(clone_path)

    _run_git(
        [
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--sparse",
            "--branch",
            EKEY_REE_PAK_TOOL_REF,
            _ree_pak_tool_repository_url(),
            str(clone_path),
        ]
    )
    _run_git(["sparse-checkout", "set", "Projects"], cwd=clone_path)


def _run_git(args: list[str], cwd: Path | None = None) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").strip()
    command = "git " + " ".join(args)
    if detail:
        raise ProfileUpdateError(f"{command} failed: {detail}")
    raise ProfileUpdateError(f"{command} failed with exit code {completed.returncode}.")


def _iter_text_file_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        yield from handle


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8", newline="\n")
    tmp_path.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
