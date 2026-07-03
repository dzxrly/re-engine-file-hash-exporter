from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

from .core.constants import IGNORED_RESOURCE_EXTENSIONS, LANGUAGE_MODE_LOCALIZED, LANGUAGE_MODES
from .core.models import BruteForceOptions, BruteForceProgress, DmpScanResult
from .core.workflow import ExportWorkflow

CANDIDATE_MODES = ("small_range", "adaptive", "custom", "auto_detect")
DEFAULT_PAK_GLOB = "*.[pP][aA][kK]"
SELECT_ALL_MISSING = "all_missing"
SELECT_ALL = "all"
RichProgressFactory = Callable[[], Any]


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class CliStep2Settings:
    selected_extensions: list[str] | str | None = None
    min_version: int = 0
    max_version: int = 4096
    mode: str = "small_range"
    custom_versions: str = ""
    neighbor_radius: int = 32
    date_start: str = ""
    date_end: str = ""
    processes: int = 0
    include_platform_suffixes: bool = True
    language_mode: str = LANGUAGE_MODE_LOCALIZED
    include_streaming: bool = True
    request_gpu: bool = False
    gpu_batch_size: int = 16384
    gpu_devices: list[int] = field(default_factory=list)
    gpu_batch_sizes: dict[int, int] = field(default_factory=dict)
    gpu_workers_per_device: int = 1
    gpu_producers_per_device: int = 0
    gpu_prefetch_batches_per_device: int = 2
    include_versioned_extensions: bool = False


@dataclass(slots=True)
class CliConfig:
    config_path: Path
    base_dir: Path
    dmp_path: Path
    output_path: Path
    pak_paths: list[Path]
    run_step2: bool
    step2: CliStep2Settings


class Step2ProgressRenderer:
    def __init__(
        self,
        progress_factory: RichProgressFactory | None = None,
        fallback_stream: TextIO | None = None,
        error_stream: TextIO | None = None,
    ) -> None:
        self.progress_factory = progress_factory or _create_rich_progress
        self.fallback_stream = fallback_stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        self.progress: Any | None = None
        self.task_id: Any | None = None
        self.using_rich = False

    def __enter__(self) -> "Step2ProgressRenderer":
        try:
            self.progress = self.progress_factory()
        except ModuleNotFoundError as exc:
            if exc.name == "rich":
                print(
                    "Rich is not installed; falling back to line-by-line Step 2 progress. "
                    "Install dependencies with `pip install -r requirements.txt` for the fixed progress bar.",
                    file=self.error_stream,
                )
                return self
            raise

        self.progress.start()
        self.task_id = self.progress.add_task(
            "Step 2",
            total=100.0,
            completed=0.0,
            phase="Preparing",
            bar=_format_ascii_bar(0.0),
            extensions="ext 0/0",
            scans="scans 0/0",
        )
        self.using_rich = True
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self.progress is not None:
            self.progress.stop()

    def __call__(self, message: object) -> None:
        if isinstance(message, BruteForceProgress):
            self.update(message)
            return
        self.log(str(message))

    def log(self, message: str) -> None:
        if self.using_rich and self.progress is not None:
            self.progress.console.print(message.rstrip())
            return
        print(message, file=self.fallback_stream, flush=True)

    def update(self, progress: BruteForceProgress) -> None:
        if not self.using_rich or self.progress is None or self.task_id is None:
            print(_format_progress_line(progress), file=self.fallback_stream, flush=True)
            return

        self.progress.update(
            self.task_id,
            completed=max(0.0, min(100.0, progress.percent)),
            phase=_format_progress_phase(progress),
            bar=_format_ascii_bar(progress.percent),
            extensions=f"ext {progress.completed_extensions}/{progress.total_extensions}",
            scans=f"scans {progress.completed_scan_count:,}/{progress.total_scan_count:,}",
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_cli_config(args.config_path)
        return run_configured_workflow(config)
    except ConfigError as err:
        print(f"Config error: {err}", file=sys.stderr)
        return 2
    except Exception as err:
        if args.verbose:
            raise
        print(f"Error: {err}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="re-file-hash-exporter cli",
        description="Run Step 1 and Step 2 from a TOML CLI config file.",
    )
    parser.add_argument(
        "config_path",
        help="TOML config file path. Relative paths inside the config are resolved from this file's directory.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show Python tracebacks for unexpected errors.")
    return parser


def load_cli_config(config_path: str | Path) -> CliConfig:
    config_path, base_dir = _resolve_config_path(config_path)
    data = _read_toml(config_path)
    step2_data = _merged_step2_data(data)

    dmp_path = _required_path(data, "dmp_path", base_dir)
    if not dmp_path.is_file():
        raise ConfigError(f"dmp_path does not exist or is not a file: {dmp_path}")

    output_path = _optional_path(data, "output_path", base_dir, default=base_dir / "config.toml")
    run_step2 = _get_bool(data, step2_data, "run_step2", True)
    pak_paths = _collect_pak_paths(data, base_dir)
    if run_step2 and not pak_paths:
        raise ConfigError("run_step2 is true, but no PAK files were configured or found.")

    settings = CliStep2Settings(
        selected_extensions=_get_selected_extensions(data, step2_data),
        min_version=_get_int(data, step2_data, "min_version", 0),
        max_version=_get_int(data, step2_data, "max_version", 4096),
        mode=_get_choice(data, step2_data, "mode", "small_range", CANDIDATE_MODES),
        custom_versions=_get_str(data, step2_data, "custom_versions", ""),
        neighbor_radius=_get_int(data, step2_data, "neighbor_radius", 32),
        date_start=_get_str(data, step2_data, "date_start", ""),
        date_end=_get_str(data, step2_data, "date_end", ""),
        processes=_get_int(data, step2_data, "processes", 0),
        include_platform_suffixes=_get_bool(data, step2_data, "include_platform_suffixes", True),
        language_mode=_get_choice(data, step2_data, "language_mode", LANGUAGE_MODE_LOCALIZED, LANGUAGE_MODES),
        include_streaming=_get_bool(data, step2_data, "include_streaming", True),
        request_gpu=_get_bool(data, step2_data, "request_gpu", False),
        gpu_batch_size=_get_int(data, step2_data, "gpu_batch_size", 16384),
        gpu_devices=_get_gpu_devices(data, step2_data),
        gpu_batch_sizes=_get_gpu_batch_sizes(data, step2_data),
        gpu_workers_per_device=_get_int(data, step2_data, "gpu_workers_per_device", 1),
        gpu_producers_per_device=_get_int(data, step2_data, "gpu_producers_per_device", 0),
        gpu_prefetch_batches_per_device=_get_int(data, step2_data, "gpu_prefetch_batches_per_device", 2),
        include_versioned_extensions=_get_bool(data, step2_data, "include_versioned_extensions", False),
    )
    _validate_step2_settings(settings)

    return CliConfig(
        config_path=config_path,
        base_dir=base_dir,
        dmp_path=dmp_path,
        output_path=output_path,
        pak_paths=pak_paths,
        run_step2=run_step2,
        step2=settings,
    )


def run_configured_workflow(config: CliConfig) -> int:
    print(f"Reading CLI config: {config.config_path}")
    workflow = ExportWorkflow()

    print("Step 1: scanning DMP and writing config...")
    scan = workflow.run_simple_export(config.dmp_path, config.output_path, progress=_print_progress)
    _print_scan_summary(scan)

    if not config.run_step2:
        print("Step 2: skipped because run_step2=false.")
        return 0

    selected = select_extensions_from_scan(
        scan,
        config.step2.selected_extensions,
        include_versioned_extensions=config.step2.include_versioned_extensions,
    )
    if not selected:
        print("Step 2: skipped because no selectable extensions were found after Step 1.")
        return 0

    options = build_bruteforce_options(config.step2, selected)
    print(
        "Step 2: brute-force matching "
        f"{len(selected)} extension(s) against {len(config.pak_paths)} PAK file(s)..."
    )
    with Step2ProgressRenderer() as step2_progress:
        result = workflow.run_bruteforce(
            config.pak_paths,
            config.output_path,
            options,
            progress=step2_progress,
        )
    if result.cancelled:
        print(f"Step 2 stopped with {len(result.matches)} partial match(es).")
    else:
        print(f"Step 2 finished with {len(result.matches)} matched path(s).")
    print(f"Output config: {config.output_path}")
    return 0


def select_extensions_from_scan(
    scan: DmpScanResult,
    configured: list[str] | str | None,
    include_versioned_extensions: bool = False,
) -> list[str]:
    if configured is None or configured == SELECT_ALL_MISSING:
        extensions = set(scan.unversioned_paths)
    elif configured == SELECT_ALL:
        extensions = set(scan.unversioned_paths)
        if include_versioned_extensions:
            extensions.update(scan.versioned_paths)
    else:
        extensions = set(configured)

    return sorted(
        extension.lower().lstrip(".")
        for extension in extensions
        if extension and extension.lower().lstrip(".") not in IGNORED_RESOURCE_EXTENSIONS
    )


def build_bruteforce_options(settings: CliStep2Settings, selected_extensions: list[str]) -> BruteForceOptions:
    return BruteForceOptions(
        selected_extensions=selected_extensions,
        min_version=settings.min_version,
        max_version=settings.max_version,
        mode=settings.mode,
        custom_versions=settings.custom_versions,
        neighbor_radius=settings.neighbor_radius,
        date_start=settings.date_start,
        date_end=settings.date_end,
        processes=settings.processes,
        include_platform_suffixes=settings.include_platform_suffixes,
        include_languages=settings.language_mode != "off",
        language_mode=settings.language_mode,
        include_streaming=settings.include_streaming,
        request_gpu=settings.request_gpu,
        gpu_batch_size=settings.gpu_batch_size,
        gpu_devices=settings.gpu_devices,
        gpu_batch_sizes=settings.gpu_batch_sizes,
        gpu_workers_per_device=settings.gpu_workers_per_device,
        gpu_producers_per_device=settings.gpu_producers_per_device,
        gpu_prefetch_batches_per_device=settings.gpu_prefetch_batches_per_device,
        include_versioned_extensions=settings.include_versioned_extensions,
    )


def _create_rich_progress() -> Any:
    from rich.console import Console
    from rich.progress import (
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    return Progress(
        TextColumn("[bold cyan]{task.fields[phase]}[/]"),
        TextColumn("{task.fields[bar]}"),
        TaskProgressColumn(),
        TextColumn("{task.fields[extensions]}"),
        TextColumn("{task.fields[scans]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=Console(),
        expand=True,
        transient=False,
    )


def _resolve_config_path(config_path: str | Path) -> tuple[Path, Path]:
    path = Path(config_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve(strict=False)
    if path.is_dir():
        raise ConfigError(f"Config path must be a TOML file, not a directory: {path}")
    if path.suffix.lower() != ".toml":
        raise ConfigError(f"Config path must point to a .toml file: {path}")
    return path, path.parent


def _read_toml(config_path: Path) -> dict[str, Any]:
    if tomllib is None:
        raise ConfigError("TOML config requires Python 3.11+ or the `tomli` package.")
    if not config_path.is_file():
        raise ConfigError(f"Config file does not exist: {config_path}")
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:  # type: ignore[union-attr]
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a TOML table: {config_path}")
    return data


def _merged_step2_data(data: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for section_name in ("bruteforce", "step2"):
        section = data.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise ConfigError(f"`{section_name}` must be a TOML table.")
        merged.update(section)
    return merged


def _collect_pak_paths(data: dict[str, Any], base_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for text in _string_list(data.get("pak_paths"), "pak_paths"):
        paths.extend(_resolve_path_pattern(text, base_dir))

    pak_glob = _value_as_str(data.get("pak_glob", DEFAULT_PAK_GLOB), "pak_glob")
    pak_dirs = _string_list(data.get("pak_dirs"), "pak_dirs")
    pak_dir = data.get("pak_dir")
    if pak_dir is not None:
        pak_dirs.extend(_string_list(pak_dir, "pak_dir"))

    for text in pak_dirs:
        if _has_glob_pattern(text):
            paths.extend(_resolve_path_pattern(text, base_dir))
            continue
        directory = _resolve_path(text, base_dir)
        if not directory.is_dir():
            raise ConfigError(f"PAK directory does not exist: {directory}")
        paths.extend(sorted(path for path in directory.glob(pak_glob) if path.is_file()))

    return _dedupe_existing_files(paths, "PAK")


def _dedupe_existing_files(paths: list[Path], label: str) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve(strict=False)
        if not resolved.is_file():
            raise ConfigError(f"{label} file does not exist: {resolved}")
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _resolve_path_pattern(text: str, base_dir: Path) -> list[Path]:
    if not _has_glob_pattern(text):
        return [_resolve_path(text, base_dir)]

    pattern_path = _resolve_path(text, base_dir)
    parent = pattern_path.parent
    if not parent.is_dir():
        raise ConfigError(f"Glob parent directory does not exist: {parent}")
    return sorted(path for path in parent.glob(pattern_path.name) if path.is_file())


def _has_glob_pattern(text: str) -> bool:
    return any(char in text for char in "*?[]")


def _required_path(data: dict[str, Any], key: str, base_dir: Path) -> Path:
    value = data.get(key)
    if value is None:
        raise ConfigError(f"Missing required config key: {key}")
    return _resolve_path(_value_as_str(value, key), base_dir)


def _optional_path(data: dict[str, Any], key: str, base_dir: Path, default: Path) -> Path:
    value = data.get(key)
    if value is None:
        return default.resolve(strict=False)
    return _resolve_path(_value_as_str(value, key), base_dir)


def _resolve_path(text: str, base_dir: Path) -> Path:
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=False)


def _get_selected_extensions(data: dict[str, Any], step2_data: dict[str, Any]) -> list[str] | str | None:
    value = _get_value(data, step2_data, "selected_extensions", None)
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        lowered = text.lower()
        if lowered in {SELECT_ALL_MISSING, "missing", "auto"}:
            return SELECT_ALL_MISSING
        if lowered == SELECT_ALL:
            return SELECT_ALL
        return _normalize_extensions(_split_csv(text), "selected_extensions")
    if isinstance(value, list):
        return _normalize_extensions(value, "selected_extensions")
    raise ConfigError("selected_extensions must be a string or a string array.")


def _normalize_extensions(values: list[Any], key: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ConfigError(f"{key} must contain only strings.")
        extension = value.strip().lower().lstrip(".")
        if not extension or extension in IGNORED_RESOURCE_EXTENSIONS or extension in seen:
            continue
        seen.add(extension)
        normalized.append(extension)
    return normalized


def _split_csv(text: str) -> list[str]:
    return [part.strip() for part in text.replace("\n", ",").split(",") if part.strip()]


def _validate_step2_settings(settings: CliStep2Settings) -> None:
    if settings.min_version < 0:
        raise ConfigError("min_version must be >= 0.")
    if settings.max_version < settings.min_version and settings.mode != "auto_detect":
        raise ConfigError("max_version must be >= min_version.")
    if settings.neighbor_radius < 0:
        raise ConfigError("neighbor_radius must be >= 0.")
    if settings.processes < 0:
        raise ConfigError("processes must be >= 0.")
    if settings.gpu_batch_size <= 0:
        raise ConfigError("gpu_batch_size must be a positive integer.")
    if any(device < 0 for device in settings.gpu_devices):
        raise ConfigError("gpu_devices must contain non-negative CUDA device indexes.")
    if settings.gpu_workers_per_device <= 0:
        raise ConfigError("gpu_workers_per_device must be a positive integer.")
    if settings.gpu_producers_per_device < 0:
        raise ConfigError("gpu_producers_per_device must be >= 0.")
    if settings.gpu_prefetch_batches_per_device < 0:
        raise ConfigError("gpu_prefetch_batches_per_device must be >= 0.")
    if any(size <= 0 for size in settings.gpu_batch_sizes.values()):
        raise ConfigError("gpu_batch_sizes values must be positive integers.")
    if any(device < 0 for device in settings.gpu_batch_sizes):
        raise ConfigError("gpu_batch_sizes device indexes must be non-negative.")


def _get_value(data: dict[str, Any], step2_data: dict[str, Any], key: str, default: Any) -> Any:
    if key in step2_data:
        return step2_data[key]
    return data.get(key, default)


def _get_str(data: dict[str, Any], step2_data: dict[str, Any], key: str, default: str) -> str:
    return _value_as_str(_get_value(data, step2_data, key, default), key)


def _get_int(data: dict[str, Any], step2_data: dict[str, Any], key: str, default: int) -> int:
    value = _get_value(data, step2_data, key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer.")
    return value


def _get_bool(data: dict[str, Any], step2_data: dict[str, Any], key: str, default: bool) -> bool:
    value = _get_value(data, step2_data, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{key} must be a boolean.")


def _get_gpu_devices(data: dict[str, Any], step2_data: dict[str, Any]) -> list[int]:
    value = _get_value(data, step2_data, "gpu_devices", [])
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip().lower()
        if not text or text == "auto":
            return []
        parts = _split_csv(text)
        return _unique_int_list(parts, "gpu_devices")
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, list):
        return _unique_int_list(value, "gpu_devices")
    raise ConfigError("gpu_devices must be `auto`, an integer, or an integer array.")


def _get_gpu_batch_sizes(data: dict[str, Any], step2_data: dict[str, Any]) -> dict[int, int]:
    value = _get_value(data, step2_data, "gpu_batch_sizes", {})
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        out: dict[int, int] = {}
        for part in _split_csv(value):
            if ":" not in part:
                raise ConfigError("gpu_batch_sizes string entries must use `device:size` format.")
            device_text, size_text = part.split(":", 1)
            out[_parse_int(device_text.strip(), "gpu_batch_sizes device")] = _parse_int(
                size_text.strip(),
                "gpu_batch_sizes size",
            )
        return out
    if isinstance(value, dict):
        out = {}
        for device, size in value.items():
            out[_parse_int(str(device), "gpu_batch_sizes device")] = _parse_int(size, "gpu_batch_sizes size")
        return out
    raise ConfigError("gpu_batch_sizes must be a string or a TOML table.")


def _unique_int_list(values: list[Any], key: str) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        parsed = _parse_int(value, key)
        if parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


def _parse_int(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{key} must be an integer.") from exc
    raise ConfigError(f"{key} must be an integer.")


def _get_choice(
    data: dict[str, Any],
    step2_data: dict[str, Any],
    key: str,
    default: str,
    choices: Sequence[str],
) -> str:
    value = _value_as_str(_get_value(data, step2_data, key, default), key)
    if value not in choices:
        allowed = ", ".join(choices)
        raise ConfigError(f"{key} must be one of: {allowed}")
    return value


def _string_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a string or a string array.")
    return [_value_as_str(item, key) for item in value]


def _value_as_str(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string.")
    return value


def _print_progress(message: object) -> None:
    if isinstance(message, BruteForceProgress):
        print(_format_progress_line(message), flush=True)
        return
    print(str(message), flush=True)


def _format_progress_line(progress: BruteForceProgress) -> str:
    detail = f" | {progress.phase_detail}" if progress.phase_detail else ""
    return (
        f"[{progress.phase}{detail}] {progress.percent:.1f}% "
        f"extensions {progress.completed_extensions}/{progress.total_extensions}, "
        f"scans {progress.completed_scan_count:,}/{progress.total_scan_count:,}"
    )


def _format_progress_phase(progress: BruteForceProgress) -> str:
    labels = {
        "loading_paks": "Loading PAKs",
        "planning": "Planning",
        "searching": "Searching",
    }
    phase = labels.get(progress.phase, progress.phase or "Step 2")
    detail = progress.phase_detail.strip()
    if not detail:
        return phase
    if detail.lower().startswith(phase.lower()):
        return detail
    return f"{phase}: {detail}"


def _format_ascii_bar(percent: float, width: int = 28) -> str:
    bounded = max(0.0, min(100.0, percent))
    filled = int(round((bounded / 100.0) * width))
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def _print_scan_summary(scan: DmpScanResult) -> None:
    print(
        "Step 1 finished: "
        f"{scan.detected_extension_count} versioned extension(s), "
        f"{scan.unversioned_extension_count} missing extension(s), "
        f"{scan.unversioned_unique_path_count} raw path(s) without suffixes.",
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
