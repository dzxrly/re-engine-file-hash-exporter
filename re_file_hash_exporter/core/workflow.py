from __future__ import annotations

from pathlib import Path
from typing import Callable

from .bruteforce import brute_force_suffixes
from .config_builder import merge_suffix_counts, write_config, write_missing_report
from .dmp_scanner import scan_dmp_file
from .models import BruteForceOptions, BruteForceResult, DmpScanResult, SuffixCounts

ProgressCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]


class ExportWorkflow:
    def __init__(self) -> None:
        self.scan_result: DmpScanResult | None = None
        self.suffix_counts: SuffixCounts = {}

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
            progress(f"Wrote config: {output_path}")
            progress(f"Wrote missing report: {report_path}")
        return scan

    def run_bruteforce(
        self,
        pak_paths: list[Path],
        output_path: Path,
        options: BruteForceOptions,
        progress: ProgressCallback | None = None,
        cancel_requested: CancelCallback | None = None,
    ) -> BruteForceResult:
        if self.scan_result is None:
            raise RuntimeError("Run step 1 before brute forcing suffixes.")

        result = brute_force_suffixes(
            self.scan_result,
            pak_paths,
            self.suffix_counts,
            options,
            progress=progress,
            cancel_requested=cancel_requested,
        )
        if result.cancelled:
            if progress:
                progress(
                    f"Brute force stopped. {len(result.matches)} partial match(es) were kept in the log but not merged into config."
                )
            return result

        if result.matches:
            self.suffix_counts = merge_suffix_counts(self.suffix_counts, result.versions_by_extension())
            write_config(output_path, self.suffix_counts)
            if progress:
                progress(f"Merged {len(result.matches)} matched paths and rewrote config: {output_path}")
        elif progress:
            progress("No brute-force matches found.")

        for warning in result.warnings:
            if progress:
                progress(f"Warning: {warning}")
        return result
