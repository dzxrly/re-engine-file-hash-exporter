from __future__ import annotations

from pathlib import Path
from typing import Callable

from .config.builder import merge_suffix_counts, write_config, write_missing_report
from .dmp.scanner import scan_dmp_file
from .models import SuffixDiscoveryOptions, SuffixDiscoveryResult, DmpScanResult, SuffixCounts
from .pak.cache import PakHashCache
from .suffix_discovery.engine import discover_suffixes

ProgressCallback = Callable[[object], None]
CancelCallback = Callable[[], bool]


class ExportWorkflow:
    def __init__(self) -> None:
        self.scan_result: DmpScanResult | None = None
        self.suffix_counts: SuffixCounts = {}
        self.pak_cache = PakHashCache()

    def run_simple_export(
        self,
        dmp_path: Path,
        output_path: Path,
        progress: ProgressCallback | None = None,
    ) -> DmpScanResult:
        scan = scan_dmp_file(dmp_path, progress=progress)
        self.scan_result = scan
        self.suffix_counts = merge_suffix_counts(scan.suffix_counts)
        write_config(output_path, self.suffix_counts)
        report_path = write_missing_report(output_path, scan)
        if progress:
            for warning in scan.warnings:
                progress(f"Warning: {warning}")
            progress(f"Wrote config: {output_path}")
            progress(f"Wrote missing report: {report_path}")
        return scan

    def run_suffix_discovery(
        self,
        pak_paths: list[Path],
        output_path: Path,
        options: SuffixDiscoveryOptions,
        progress: ProgressCallback | None = None,
        cancel_requested: CancelCallback | None = None,
    ) -> SuffixDiscoveryResult:
        if self.scan_result is None:
            raise RuntimeError("Run step 1 before suffix discovery.")

        result = discover_suffixes(
            self.scan_result,
            pak_paths,
            self.suffix_counts,
            options,
            progress=progress,
            cancel_requested=cancel_requested,
            pak_cache=self.pak_cache,
        )
        if result.cancelled:
            found_versions = result.versions_by_extension()
            version_count = sum(len(versions) for versions in found_versions.values())
            if found_versions:
                self.suffix_counts = merge_suffix_counts(self.suffix_counts, found_versions)
                write_config(output_path, self.suffix_counts)
            if progress:
                if found_versions:
                    progress(
                        f"Suffix discovery stopped. Merged {version_count} suffix version(s) from "
                        f"{len(result.matches)} partial evidence match(es) and rewrote config: {output_path}"
                    )
                else:
                    progress("Suffix discovery stopped. No partial matches were found to merge into config.")
                for warning in result.warnings:
                    progress(f"Warning: {warning}")
            return result

        if result.matches:
            self.suffix_counts = merge_suffix_counts(self.suffix_counts, result.versions_by_extension())
            write_config(output_path, self.suffix_counts)
            if progress:
                progress(f"Merged {len(result.matches)} suffix evidence match(es) and rewrote config: {output_path}")
        elif progress:
            progress("No suffix evidence matches found.")

        for warning in result.warnings:
            if progress:
                progress(f"Warning: {warning}")
        return result
