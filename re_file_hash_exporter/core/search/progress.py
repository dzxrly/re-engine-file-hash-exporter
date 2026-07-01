from __future__ import annotations

import time
from typing import Callable

from ..models import BruteForceProgress

ProgressCallback = Callable[[object], None]


class BruteForceProgressTracker:
    def __init__(
        self,
        progress: ProgressCallback | None,
        total_extensions: int,
        total_scan_count: int,
        phase: str = "searching",
        phase_detail: str = "",
    ) -> None:
        self.progress = progress
        self.total_extensions = total_extensions
        self.total_scan_count = total_scan_count
        self.completed_extensions = 0
        self.completed_scan_count = 0
        self.current_extension = ""
        self.phase = phase
        self.phase_detail = phase_detail
        self.started_at = time.monotonic()
        self.last_emit_at = 0.0

    def set_phase(
        self,
        phase: str,
        phase_detail: str = "",
        total_extensions: int | None = None,
        total_scan_count: int | None = None,
        completed_extensions: int = 0,
        completed_scan_count: int = 0,
    ) -> None:
        self.phase = phase
        self.phase_detail = phase_detail
        if total_extensions is not None:
            self.total_extensions = total_extensions
        if total_scan_count is not None:
            self.total_scan_count = total_scan_count
        self.completed_extensions = completed_extensions
        self.completed_scan_count = completed_scan_count
        self.current_extension = ""
        self.emit(force=True)

    def set_planning_progress(
        self,
        completed_extensions: int,
        planned_scan_count: int,
        current_extension: str,
        force: bool = False,
    ) -> None:
        self.phase = "planning"
        self.completed_extensions = min(self.total_extensions, completed_extensions)
        self.completed_scan_count = max(0, planned_scan_count)
        self.current_extension = current_extension
        self.phase_detail = f"Planning .{current_extension}" if current_extension else "Planning candidates"
        self.emit(force=force)

    def advance_scans(self, count: int, current_extension: str) -> None:
        if count <= 0:
            return
        self.phase = "searching"
        self.phase_detail = f"Searching .{current_extension}" if current_extension else "Searching"
        self.completed_scan_count = min(self.total_scan_count, self.completed_scan_count + count)
        self.current_extension = current_extension
        self.emit()

    def finish_extension(self, current_extension: str) -> None:
        self.phase = "searching"
        self.phase_detail = f"Finished .{current_extension}" if current_extension else "Searching"
        self.completed_extensions = min(self.total_extensions, self.completed_extensions + 1)
        if self.completed_extensions >= self.total_extensions:
            self.completed_scan_count = self.total_scan_count
        self.current_extension = current_extension
        self.emit(force=True)

    def emit(self, force: bool = False) -> None:
        if not self.progress:
            return
        now = time.monotonic()
        is_complete = (
            self.completed_extensions >= self.total_extensions
            or self.completed_scan_count >= self.total_scan_count > 0
        )
        if not force and not is_complete and now - self.last_emit_at < 0.2:
            return
        self.last_emit_at = now
        self.progress(
            BruteForceProgress(
                completed_extensions=self.completed_extensions,
                total_extensions=self.total_extensions,
                completed_scan_count=self.completed_scan_count,
                total_scan_count=self.total_scan_count,
                elapsed_seconds=now - self.started_at,
                current_extension=self.current_extension,
                phase=self.phase,
                phase_detail=self.phase_detail,
            )
        )
