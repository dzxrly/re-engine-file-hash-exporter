from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, DefaultDict

from ..models import DmpScanResult
from .parser import (
    accept_char,
    looks_like_game_resource_path,
    resource_suffix_from_path,
    versioned_raw_path_from_path,
)

ProgressCallback = Callable[[str], None]

_RIGHT_BOUNDARY_TEXT = "text"
_RIGHT_BOUNDARY_BINARY = "binary"
_RIGHT_BOUNDARY_BUFFER_END = "buffer_end"
_SAFE_TEXT_DELIMITERS = frozenset((0x00, 0x09, 0x0A, 0x0D, ord('"')))


def extract_wide_path_span(buf: bytes, slash_pos: int) -> tuple[str, int, int] | None:
    begin = slash_pos
    while begin >= 2 and accept_char(buf[begin - 2]) and buf[begin - 1] == 0:
        begin -= 2
    if begin == slash_pos:
        return None

    end = slash_pos + 2
    while end + 1 < len(buf) and accept_char(buf[end]) and buf[end + 1] == 0:
        end += 2
    if end == slash_pos:
        return None

    try:
        return buf[begin:end].decode("utf-16le"), begin, end
    except UnicodeDecodeError:
        return None


def extract_wide_path(buf: bytes, slash_pos: int) -> str | None:
    span = extract_wide_path_span(buf, slash_pos)
    return span[0] if span else None


def _wide_path_right_boundary(buf: bytes, end: int) -> str:
    if end + 1 >= len(buf):
        return _RIGHT_BOUNDARY_BUFFER_END
    if buf[end + 1] == 0 and buf[end] in _SAFE_TEXT_DELIMITERS:
        return _RIGHT_BOUNDARY_TEXT
    return _RIGHT_BOUNDARY_BINARY


def _format_suspicious_suffix_warnings(
    suspicious: Counter[tuple[str, int, str]],
) -> list[str]:
    reason_text = {
        _RIGHT_BOUNDARY_BINARY: "followed by non-ASCII/binary data instead of a UTF-16 text boundary",
        _RIGHT_BOUNDARY_BUFFER_END: "reached the scan buffer boundary before a UTF-16 text boundary",
    }
    warnings: list[str] = []
    for (extension, version, reason), count in sorted(suspicious.items()):
        occurrence = "occurrence" if count == 1 else "occurrences"
        warnings.append(
            f"Ignored suspicious DMP path fragment ending in .{extension}.{version} "
            f"({count} {occurrence}; {reason_text[reason]})."
        )
    return warnings


def scan_dmp_file(
    dmp_path: Path,
    progress: ProgressCallback | None = None,
    chunk_size: int = 64 * 1024 * 1024,
    overlap: int = 16 * 1024,
) -> DmpScanResult:
    if not dmp_path.is_file():
        raise FileNotFoundError(f"DMP file not found: {dmp_path}")

    slash_u16 = b"/\x00"
    counts: DefaultDict[str, Counter[int]] = defaultdict(Counter)
    unversioned: DefaultDict[str, Counter[str]] = defaultdict(Counter)
    versioned: DefaultDict[str, Counter[str]] = defaultdict(Counter)
    suspicious_suffixes: Counter[tuple[str, int, str]] = Counter()
    pos = 0
    processed_until = 0
    prev = b""
    file_size = dmp_path.stat().st_size

    with dmp_path.open("rb") as handle:
        while True:
            data = handle.read(chunk_size)
            if not data:
                break

            buf = prev + data
            base = pos - len(prev)
            buffer_end = pos + len(data)
            final_chunk = buffer_end >= file_size
            safe_scan_end = buffer_end if final_chunk else max(processed_until, buffer_end - overlap)
            start = 0

            while True:
                slash_pos = buf.find(slash_u16, start)
                if slash_pos < 0:
                    break

                next_start = slash_pos + 2
                absolute_slash_pos = base + slash_pos
                if processed_until <= absolute_slash_pos < safe_scan_end:
                    span = extract_wide_path_span(buf, slash_pos)
                    if span:
                        path, _begin, end = span
                        next_start = end
                        suffix, missing = resource_suffix_from_path(path)
                        boundary = _wide_path_right_boundary(buf, end)
                        if boundary != _RIGHT_BOUNDARY_TEXT:
                            if suffix:
                                extension, version = suffix
                                suspicious_suffixes[(extension, version, boundary)] += 1
                            start = next_start
                            continue
                        if suffix:
                            extension, version = suffix
                            counts[extension][version] += 1
                            versioned_raw = versioned_raw_path_from_path(path)
                            if versioned_raw and looks_like_game_resource_path(path):
                                raw_extension, _raw_version, raw_path = versioned_raw
                                versioned[raw_extension][raw_path] += 1
                        elif missing and looks_like_game_resource_path(path):
                            extension, normalized = missing
                            unversioned[extension][normalized] += 1

                start = next_start

            processed_until = safe_scan_end
            pos = buffer_end
            prev = buf[-overlap:] if overlap > 0 else b""
            if progress:
                progress(f"Scanning {dmp_path.name}: {pos / max(file_size, 1):.0%}")

    return DmpScanResult(
        dmp_files=[dmp_path],
        suffix_counts=dict(counts),
        unversioned_paths=dict(unversioned),
        versioned_paths=dict(versioned),
        scanned_bytes=pos,
        warnings=_format_suspicious_suffix_warnings(suspicious_suffixes),
    )
